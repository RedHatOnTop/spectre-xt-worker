"""Private operator expectations and structured coordinator dispositions."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat

from . import disposition, native
from .storage import private_file


def read_private(path):
    path = Path(path).absolute()
    if path != path.resolve():
        raise ValueError('settings must be a private regular file without symlinks')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        entry = os.fstat(descriptor)
        if (not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid()
                or stat.S_IMODE(entry.st_mode) & 0o077):
            raise ValueError('settings must be a private regular file owned by the caller')
        with os.fdopen(descriptor, encoding='utf-8', closefd=False) as source:
            return json.load(source)
    finally:
        os.close(descriptor)


def config(path):
    value = read_private(path)
    if (not isinstance(value, dict) or not {'schema_version', 'coordinators'} <= set(value)
            or set(value) - {'schema_version', 'coordinators', 'intake', 'offload'} or value['schema_version'] != 1):
        raise ValueError('continuity settings require schema_version 1 and coordinators')
    rows = value['coordinators']
    if not isinstance(rows, list):
        raise ValueError('coordinators must be a list of approved native targets')
    for row in rows:
        native.validate_payload(row)
    for field in ('run_id', 'terminal_handle', 'worktree_id'):
        if len({r[field] for r in rows}) != len(rows):
            raise ValueError('duplicate coordinator owner in continuity settings')
    intake = value.get('intake', [])
    if not isinstance(intake, list):
        raise ValueError('intake must be a list of approved source bindings')
    for binding in intake:
        if (not isinstance(binding, dict) or set(binding) != {'run_id', 'path'}
                or binding['run_id'] not in {r['run_id'] for r in rows}
                or not isinstance(binding['path'], str) or not Path(binding['path']).is_absolute()):
            raise ValueError('intake requires an absolute ledger path and approved Run')
    from .offload_transport import STUDIO
    offload = value.get('offload', [])
    if not isinstance(offload, list):
        raise ValueError('offload requires approved existing Studio bindings')
    for binding in offload:
        if (not isinstance(binding, dict) or set(binding) != {'run_id', 'studio'}
                or binding['run_id'] not in {r['run_id'] for r in rows}
                or not isinstance(binding['studio'], str) or not STUDIO.fullmatch(binding['studio'])):
            raise ValueError('offload requires an approved Run and exact existing Studio')
    if len({r['run_id'] for r in offload}) != len(offload):
        raise ValueError('offload requires one existing Studio per Run')
    return value


def dispositions(path):
    try:
        value = read_private(path)
    except FileNotFoundError:
        return {}
    return disposition.validate(value)


@contextmanager
def lock(path):
    descriptor = os.open(private_file(path), os.O_RDWR | os.O_NOFOLLOW)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield
    finally:
        os.close(descriptor)
