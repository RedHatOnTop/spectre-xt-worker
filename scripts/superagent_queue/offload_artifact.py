"""Snapshot exact returned artifacts independently of wrapper delivery and native settlement."""
import hashlib
import importlib.util
import json
from pathlib import Path

from . import assignment, execution, offload_assignment, offload_receipt, offload_recovery
from .model import encoded, identifier, timestamp
from .settings import read_private

HELPER = Path(__file__).resolve().parents[1] / 'offload-artifacts.py'
HELPER_SHA256 = '4f551ecc66c3d8e2895730c5d2ad9e65f8413196b13cf817884f214f10222ab4'
FIELDS = frozenset(('schema_version', 'intent_id', 'wrapper_exit_sha256', 'remote_result_sha256',
                    'artifact_root', 'manifest', 'verification'))
PROOF_FIELDS = frozenset(('schema_version', 'job_id', 'command_sha256', 'manifest_sha256',
                          'verified_at', 'native_settlement'))


def helper():
    if HELPER.absolute() != HELPER.resolve() or hashlib.sha256(HELPER.read_bytes()).hexdigest() != HELPER_SHA256:
        raise ValueError('artifact helper differs from the reviewed bytes')
    spec = importlib.util.spec_from_file_location('spectre_offload_artifact_helper', HELPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def remote_result(record):
    result = (record['reconciliation']['remote_result'] if record.get('reconciliation')
              else record['remote_result'] if record.get('remote_observation') == 'exited' else None)
    if result is None:
        raise ValueError('artifact evidence requires the exact recorded remote command exit')
    return offload_receipt.remote(result, record['job_id'], record['command_sha256'])


def packet(record):
    authority = record.get('authority')
    if not isinstance(authority, dict) or 'packet_path' not in authority:
        raise ValueError('artifact recovery requires its original native packet')
    text = assignment.packet_text(Path(authority['packet_path']))
    if hashlib.sha256(text.encode()).hexdigest() != record['binding']['packet_sha256']:
        raise ValueError('artifact packet differs from its original native binding')
    value = offload_assignment.spec(json.loads(text))
    if offload_receipt.command_digest(value['command']) != record['command_sha256']:
        raise ValueError('artifact packet command differs from its original execution')
    helper().selection(record['job_id'], record['command_sha256'], value['artifacts'])
    return value


def destination(root, record, path):
    path = Path(path).absolute()
    root = Path(root).absolute()
    expected = root.parent / 'offload-artifacts' / record['job_id']
    if path != path.resolve():
        raise ValueError('artifact snapshot has an unexpected destination')
    if path != expected:
        attempt = path.parent
        identifier(attempt.name)
        if path.name != record['job_id'] or attempt.parent != root / record['job_id'] / 'artifact-recovery':
            raise ValueError('artifact snapshot has an unexpected destination')
        claim = read_private(attempt / 'intent.json')
        expected_claim = {'schema_version': 1, 'attempt_id': attempt.name,
                          'job_id': record['job_id'], 'intent_id': record['intent_id'],
                          'command_sha256': record['command_sha256']}
        if encoded(claim) != encoded(expected_claim):
            raise ValueError('artifact recovery destination differs from its original intent')
    return path


def validate(root, record, value):
    if (not isinstance(value, dict) or set(value) != FIELDS
            or type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or value.get('intent_id') != record['intent_id']
            or value.get('wrapper_exit_sha256') != offload_recovery.wrapper_digest(record)
            or value.get('remote_result_sha256') != hashlib.sha256(encoded(remote_result(record)).encode()).hexdigest()):
        raise ValueError('artifact snapshot differs from its original execution evidence')
    destination(root, record, value['artifact_root'])
    manifest, proof = value['manifest'], value['verification']
    if (not isinstance(manifest, dict) or set(manifest) != {
            'schema_version', 'job_id', 'command_sha256', 'artifacts', 'entries'}
            or type(manifest.get('schema_version')) is not int or manifest['schema_version'] != 1
            or manifest.get('job_id') != record['job_id'] or manifest.get('command_sha256') != record['command_sha256']
            or not isinstance(manifest.get('artifacts'), list)
            or any(not isinstance(path, str) for path in manifest['artifacts'])
            or not isinstance(manifest.get('entries'), list)
            or not isinstance(proof, dict) or set(proof) != PROOF_FIELDS
            or type(proof.get('schema_version')) is not int or proof['schema_version'] != 1
            or proof.get('job_id') != record['job_id'] or proof.get('command_sha256') != record['command_sha256']
            or proof.get('manifest_sha256') != hashlib.sha256(encoded(manifest).encode()).hexdigest()
            or proof.get('native_settlement') is not False or proof.get('verified_at') is None):
        raise ValueError('artifact snapshot lacks its exact manifest verification')
    helper().selection(record['job_id'], record['command_sha256'], manifest['artifacts'])
    timestamp(proof['verified_at'])
    return value


def observe(root, record):
    directory = Path(root).absolute() / record['job_id']
    try:
        existing = read_private(directory / 'artifact-exit.json')
    except FileNotFoundError:
        pass
    else:
        return validate(root, record, existing)
    artifact_root = Path(root).absolute().parent / 'offload-artifacts' / record['job_id']
    if (not (artifact_root / 'verified.json').exists()
            or record.get('remote_observation') != 'exited' and not record.get('reconciliation')):
        return None
    return capture(root, record, artifact_root)


def capture(root, record, artifact_root):
    directory = Path(root).absolute() / record['job_id']
    artifact_root = destination(root, record, artifact_root)
    spec = packet(record)
    runner = helper()
    manifest = runner.read_private(artifact_root / 'manifest.json')
    proof = runner.verify(artifact_root, record['job_id'], record['command_sha256'], spec['artifacts'])
    value = {'schema_version': 1, 'intent_id': record['intent_id'],
             'wrapper_exit_sha256': offload_recovery.wrapper_digest(record),
             'remote_result_sha256': hashlib.sha256(encoded(remote_result(record)).encode()).hexdigest(),
             'artifact_root': str(artifact_root),
             'manifest': manifest, 'verification': proof}
    validate(root, record, value)
    try:
        execution.persist(directory, 'artifact-exit.json', value)
    except FileExistsError:
        existing = validate(root, record, read_private(directory / 'artifact-exit.json'))
        if existing != value:
            raise ValueError('artifact snapshot conflicts with its first recorded verification')
        return existing
    return value


def event(record, proof):
    return {'source': 'offload-artifact', 'event_id': 'artifact-' + record['intent_id'],
            'content': {**record['binding'], 'job_id': record['job_id'], 'admission': 'review_only',
                        'artifact_root': proof['artifact_root'], 'verification': proof['verification'],
                        'artifacts': proof['manifest']['artifacts'],
                        'wrapper_exit_sha256': proof['wrapper_exit_sha256'],
                        'remote_result_sha256': proof['remote_result_sha256'],
                        'native_settlement': False, 'capacity_released': False}}


def publish(journal, target, job_id, caller, cli, studio=None):
    from orca_runtime import observe as native
    from . import driver
    from .target import owner
    current = owner(target, native.collect(cli, run_ids=(target['run_id'],), include_workers=False))
    if (caller.get('ORCA_TERMINAL_HANDLE') != target['terminal_handle']
            or caller.get('ORCA_PANE_KEY') != current.pane_key):
        raise ValueError('artifact publication requires the exact current coordinator pane')
    root = journal.path.parent / 'offloads'
    record = offload_receipt.inspect(root, job_id)
    if record['observation'] != 'exited' or record['binding']['run_id'] != target['run_id']:
        raise ValueError('artifact publication requires its original Run and actual wrapper exit')
    if studio is not None:
        from .offload_artifact_fetch import fetch
        proof = fetch(root, job_id, studio)['artifact']
    else:
        proof = observe(root, record)
    if proof is None:
        raise ValueError('artifact publication requires verified returned artifacts')
    queued = driver.ingest_offloads(journal, target)
    return {'job_id': job_id, 'artifact': proof, 'queued': queued,
            'native_mutations': False, 'capacity_released': False}
