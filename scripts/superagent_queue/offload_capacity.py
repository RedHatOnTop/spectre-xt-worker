"""Existing shared lease protection with exact completed-job release and Studio ownership proof."""
import hashlib
import os
from pathlib import Path
import re
import stat
import subprocess
from uuid import uuid4

from . import execution, offload_receipt, offload_transport, settings
from .model import encoded, identifier
from .settings import read_private

FIELDS = frozenset(('schema_version', 'job_id', 'command_sha256', 'lease_dir', 'target', 'wrapper', 'start_marker', 'reservation'))


def marker(value, studio, teamspace):
    if (not isinstance(value, dict) or set(value) != {'schema_version', 'operation_id', 'studio', 'teamspace', 'starter'}
            or type(value.get('schema_version')) is not int or value['schema_version'] != 1
            or value.get('studio') != studio or value.get('teamspace') != teamspace):
        raise ValueError('capacity start marker does not match its original Studio')
    identifier(value['operation_id'])
    identity = value['starter']
    if (not isinstance(identity, dict) or not {'pid', 'boot_id', 'start_ticks'} <= set(identity)
            or type(identity['pid']) is not int or identity['pid'] < 1
            or type(identity['start_ticks']) is not int or identity['start_ticks'] < 0
            or not isinstance(identity['boot_id'], str) or not identity['boot_id']):
        raise ValueError('capacity start marker lacks its exact starter process')
    return value


def start_marker(leases, studio, teamspace, pid):
    leases = execution.private_directory(leases)
    value = {'schema_version': 1, 'operation_id': 'studio-' + str(uuid4()), 'studio': studio,
             'teamspace': teamspace, 'starter': execution.process(pid)}
    marker(value, studio, teamspace)
    execution.persist(leases, '.started', value)
    return value


def confirm_start(leases, value):
    leases = execution.private_directory(leases)
    if encoded(read_private(leases / '.started')) != encoded(value):
        raise ValueError('Studio start confirmation differs from its original operation')
    execution.persist(leases, '.start-confirmed', value)


def record(directory, leases, job_id, digest, target, wrapper, start):
    identifier(job_id)
    leases = execution.private_directory(leases)
    if (not isinstance(target, dict) or set(target) != {'studio', 'teamspace', 'remote_dir', 'remote_boot_id'}
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', target.get('studio', ''))
            or not isinstance(target.get('teamspace'), str) or not target['teamspace']
            or not re.fullmatch(r'[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*', target.get('remote_dir', ''))
            or any(part in ('.', '..') for part in target['remote_dir'].split('/'))
            or not isinstance(target.get('remote_boot_id'), str) or not target['remote_boot_id']
            or not re.fullmatch(r'[0-9a-f]{64}', digest)):
        raise ValueError('capacity requires exact original Studio, remote directory and command')
    if start is not None:
        marker(start, target['studio'], target['teamspace'])
    value = {'schema_version': 1, 'job_id': job_id, 'command_sha256': digest,
             'lease_dir': str(leases), 'target': target, 'wrapper': wrapper, 'start_marker': start,
             'reservation': reservation_identity(leases / ('.unverifiable-' + job_id), '~/' + target['remote_dir'] + '\n')}
    reservation_claim = Path(directory) / 'capacity-reservation'
    os.link(leases / ('.unverifiable-' + job_id), reservation_claim)
    if reservation_identity(reservation_claim, '~/' + target['remote_dir'] + '\n') != value['reservation']:
        raise ValueError('capacity reservation changed while binding its original execution')
    encoded(value)
    execution.persist(Path(directory), 'capacity.json', value)
    return value


def validate(capacity, record):
    if (not isinstance(capacity, dict) or set(capacity) != FIELDS
            or type(capacity.get('schema_version')) is not int or capacity['schema_version'] != 1
            or capacity.get('job_id') != record['job_id']
            or capacity.get('command_sha256') != record['command_sha256']
            or not isinstance(capacity.get('wrapper'), dict) or not isinstance(record.get('child'), dict)
            or any(capacity['wrapper'].get(key) != record['child'].get(key) for key in ('pid', 'boot_id', 'start_ticks'))):
        raise ValueError('capacity record differs from its original execution')
    target = capacity['target']
    if (not isinstance(target, dict) or set(target) != {'studio', 'teamspace', 'remote_dir', 'remote_boot_id'}
            or not isinstance(target.get('studio'), str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', target['studio'])
            or not isinstance(target.get('teamspace'), str) or not target['teamspace']
            or not isinstance(target.get('remote_dir'), str)
            or not re.fullmatch(r'[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*', target['remote_dir'])
            or any(part in ('.', '..') for part in target['remote_dir'].split('/'))
            or not isinstance(target.get('remote_boot_id'), str) or not target['remote_boot_id']
            or not isinstance(capacity.get('lease_dir'), str) or not Path(capacity['lease_dir']).is_absolute()):
        raise ValueError('capacity target cannot verify its original Studio')
    if capacity['start_marker'] is not None:
        marker(capacity['start_marker'], target['studio'], target['teamspace'])
    return capacity


def absent(identity):
    if (not isinstance(identity, dict) or not {'pid', 'boot_id', 'start_ticks'} <= set(identity)
            or identity['boot_id'] != execution.BOOT_ID.read_text().strip()):
        raise ValueError('capacity release requires original wrapper identity on the current boot')
    try:
        execution.process(identity['pid'])
    except FileNotFoundError:
        return
    raise ValueError('original wrapper PID is still present or reused; capacity release refused')


def provider(capacity, action):
    from .offload_provider import query
    return query(capacity['target'], action)


def other_leases(leases):
    for entry in leases.iterdir():
        if entry.name.startswith('.unverifiable-') or entry.name.isdigit():
            return True
    return False


def release_reservation(directory, capacity):
    leases = Path(capacity['lease_dir'])
    reservation = leases / ('.unverifiable-' + capacity['job_id'])
    archived = directory / 'reservation'
    expected = '~/' + capacity['target']['remote_dir'] + '\n'
    if archived.exists():
        if reservation_identity(archived, expected) != capacity['reservation']:
            raise ValueError('archived capacity reservation differs from its original job')
        if reservation.exists():
            if reservation.stat().st_ino != archived.stat().st_ino or reservation.stat().st_dev != archived.stat().st_dev:
                raise ValueError('capacity reservation was recreated; preserve the new reservation')
            reservation.unlink()
    else:
        if reservation_identity(reservation, expected) != capacity['reservation']:
            raise ValueError('capacity reservation differs from its exact original job')
        os.link(reservation, archived)
        if (reservation_identity(archived, expected) != capacity['reservation']
                or reservation_identity(reservation, expected) != capacity['reservation']):
            raise ValueError('capacity reservation changed while archiving; preserve new work')
        reservation.unlink()
    for path in (directory, leases):
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def reservation_identity(path, expected):
    if path.absolute() != path.resolve():
        raise ValueError('capacity reservation cannot contain symlinks')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        entry = os.fstat(descriptor)
        if (not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid()
                or stat.S_IMODE(entry.st_mode) != 0o600 or entry.st_size > 4096
                or os.read(descriptor, 4097).decode() != expected):
            raise ValueError('capacity reservation differs from its exact original job')
        return {'device': entry.st_dev, 'inode': entry.st_ino,
                'sha256': hashlib.sha256(expected.encode()).hexdigest()}
    finally:
        os.close(descriptor)


def studio(directory, capacity):
    leases = Path(capacity['lease_dir'])
    try:
        completed = read_private(directory / 'studio-result.json')
    except FileNotFoundError:
        pass
    else:
        expected = {'start_marker': capacity['start_marker'], 'studio_state': 'stopped'}
        if not isinstance(completed, dict) or encoded(completed) != encoded(expected):
            raise ValueError('Studio result differs from its original start identity')
        archived_stop = directory / 'studio-stop.json'
        stop = read_private(archived_stop) if archived_stop.exists() else None
        if stop is None and (leases / '.stop-intent').exists():
            stop = read_private(leases / '.stop-intent')
        archive_studio(directory, leases, capacity['start_marker'], stop)
        return 'stopped'
    if other_leases(leases):
        return 'retained_shared'
    expected = capacity['start_marker']
    if expected is None:
        return 'retained_external'
    try:
        confirmed = read_private(leases / '.start-confirmed')
    except FileNotFoundError:
        return 'retained_external'
    if not isinstance(confirmed, dict) or encoded(confirmed) != encoded(expected):
        return 'retained_external'
    try:
        current = read_private(leases / '.started')
    except FileNotFoundError:
        return 'retained_external'
    if not isinstance(current, dict) or encoded(current) != encoded(expected):
        return 'retained_external'
    stop_path = leases / '.stop-intent'
    stop = read_private(stop_path) if stop_path.exists() else None
    if stop_path.exists() and (not isinstance(stop, dict) or stop.get('start_marker') != expected
                             or stop.get('remote_boot_id') != capacity['target']['remote_boot_id']):
        return 'retained_external'
    status = provider(capacity, 'status')
    if status.get('status') == 'Stopped':
        execution.persist(directory, 'studio-result.json', {'start_marker': expected, 'studio_state': 'stopped'})
        archive_studio(directory, leases, expected, stop)
        return 'stopped'
    if (status.get('status') != 'Running' or status.get('remote_boot_id') != capacity['target']['remote_boot_id']):
        return 'retained_external'
    if stop is not None:
        return 'stop_unknown'
    stop = {'start_marker': expected, 'remote_boot_id': capacity['target']['remote_boot_id']}
    execution.persist(leases, '.stop-intent', stop)
    provider(capacity, 'stop')
    observed = provider(capacity, 'status')
    if observed.get('status') != 'Stopped':
        return 'stop_unknown'
    execution.persist(directory, 'studio-result.json', {'start_marker': expected, 'studio_state': 'stopped'})
    archive_studio(directory, leases, expected, stop)
    return 'stopped'


def archive_studio(directory, leases, start, stop):
    for name, value, archived in (('.started', start, 'studio-start.json'), ('.start-confirmed', start, 'studio-confirmed.json'),
                                  ('.stop-intent', stop, 'studio-stop.json')):
        if value is None:
            continue
        try:
            execution.persist(directory, archived, value)
        except FileExistsError:
            if encoded(read_private(directory / archived)) != encoded(value):
                raise ValueError('archived Studio operation differs from original identity')
        path = leases / name
        if not path.exists():
            continue
        if encoded(read_private(path)) != encoded(value):
            raise ValueError('shared Studio operation changed before cleanup; preserve markers')
        path.unlink()


def release(root, job_id, cleanup):
    identifier(job_id)
    job = Path(root).absolute() / job_id
    record = offload_receipt.inspect(root, job_id)
    if record['observation'] != 'exited':
        raise ValueError('capacity release requires the actual original wrapper exit')
    capacity = validate(read_private(job / 'capacity.json'), record)
    if reservation_identity(job / 'capacity-reservation', '~/' + capacity['target']['remote_dir'] + '\n') != capacity['reservation']:
        raise ValueError('capacity reservation does not match its original bound inode')
    target = offload_transport.location(job, record, capacity['target']['studio'])
    if target['remote_dir'] != capacity['target']['remote_dir']:
        raise ValueError('capacity target differs from the original transport directory')
    from .offload_artifact import remote_result
    if remote_result(record)['runtime']['boot_id'] != capacity['target']['remote_boot_id']:
        raise ValueError('capacity remote boot differs from its exact command receipt')
    if encoded(read_private(job / 'cleanup/exit.json')) != encoded(cleanup) or cleanup['receipt'].get('tree_removed') is not True:
        raise ValueError('capacity release requires its exact completed cleanup receipt')
    directory = execution.private_directory(job / 'resources')
    intent = {'capacity_sha256': hashlib.sha256(encoded(capacity).encode()).hexdigest(),
              'cleanup_sha256': hashlib.sha256(encoded(cleanup).encode()).hexdigest()}
    try:
        existing = read_private(directory / 'release.json')
    except FileNotFoundError:
        pass
    else:
        if not isinstance(existing, dict) or existing.get('intent') != intent:
            raise ValueError('capacity release differs from its original resource decision')
        return existing
    absent(capacity['wrapper'])
    leases = execution.private_directory(capacity['lease_dir'])
    with settings.lock(leases / '.lock'):
        try:
            execution.persist(directory, 'intent.json', intent)
        except FileExistsError:
            if encoded(read_private(directory / 'intent.json')) != encoded(intent):
                raise ValueError('resource release intent changed; preserve original reservation')
        try:
            release_reservation(directory, capacity)
            state = studio(directory, capacity)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            try:
                execution.persist(directory, 'failure-' + str(uuid4()) + '.json',
                                  {'intent': intent, 'error': str(exc), 'native_mutations': False})
            except (OSError, ValueError) as evidence_error:
                exc.add_note(f'resource failure evidence could not be saved: {evidence_error}')
            raise
        value = {'intent': intent, 'capacity_released': True, 'studio_state': state, 'native_mutations': False}
        if state == 'stop_unknown':
            return value
        execution.persist(directory, 'release.json', value)
        return value
