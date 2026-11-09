"""Private SQLite storage with explicit crash-durable transactions."""
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import stat

SCHEMA_VERSION = 3
BUSY_TIMEOUT_MS = 5000
SCHEMA = (
    '''CREATE TABLE event (
        source TEXT NOT NULL, event_id TEXT NOT NULL, identity TEXT NOT NULL,
        content TEXT NOT NULL, effect_key TEXT NOT NULL, created_at REAL NOT NULL,
        PRIMARY KEY (source, event_id))''',
    '''CREATE TABLE effect (
        key TEXT PRIMARY KEY, kind TEXT NOT NULL, lane TEXT NOT NULL,
        seq INTEGER UNIQUE NOT NULL, generation INTEGER NOT NULL DEFAULT 1,
        payload TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending','running','reconciling','attention','delivered')),
        failures INTEGER NOT NULL DEFAULT 0, due_at REAL NOT NULL,
        error TEXT NOT NULL DEFAULT '', claim_token TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL)''',
    '''CREATE TABLE attempt (
        token TEXT PRIMARY KEY, effect_key TEXT NOT NULL REFERENCES effect(key),
        generation INTEGER NOT NULL, owner TEXT NOT NULL, payload TEXT NOT NULL, outcome TEXT NOT NULL,
        error TEXT NOT NULL DEFAULT '', receipt TEXT, started_at REAL NOT NULL,
        finished_at REAL)''',
    'CREATE INDEX effect_lane_order ON effect(lane, seq, status)',
    'CREATE INDEX effect_ready ON effect(status, due_at, seq)',
    'CREATE INDEX attempt_effect ON attempt(effect_key, started_at)',
)
OPERATIONAL_SCHEMA = (
    '''CREATE TABLE source_cursor (
        source_key TEXT PRIMARY KEY, revision INTEGER NOT NULL,
        position TEXT NOT NULL, updated_at REAL NOT NULL)''',
    '''CREATE TABLE disposition (
        run_id TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL,
        owner TEXT NOT NULL, published_at REAL NOT NULL,
        PRIMARY KEY (run_id, revision))''',
)
MAIL_SCHEMA = (
    '''CREATE TABLE mail_obligation (
        run_id TEXT NOT NULL, message_id TEXT NOT NULL, effect_key TEXT NOT NULL REFERENCES effect(key),
        observed_at REAL NOT NULL, PRIMARY KEY (run_id, message_id))''',
    '''CREATE TABLE mail_review (
        run_id TEXT NOT NULL, message_id TEXT NOT NULL, body TEXT NOT NULL,
        reviewed_at REAL NOT NULL, PRIMARY KEY (run_id, message_id))''',
)


@contextmanager
def transaction(connection):
    connection.execute('BEGIN IMMEDIATE')
    try:
        yield
        connection.execute('COMMIT')
    except BaseException:
        connection.execute('ROLLBACK')
        raise


def private_file(path):
    path = Path(path).absolute()
    if path != path.resolve():
        raise ValueError('journal storage must be a private regular file without symlinks')
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent = path.parent.stat()
    if parent.st_uid != os.getuid() or stat.S_IMODE(parent.st_mode) & 0o077:
        raise ValueError('journal parent must be a private directory owned by the caller')
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        entry = os.fstat(descriptor)
        if not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid() or stat.S_IMODE(entry.st_mode) != 0o600:
            raise ValueError('journal storage must be a private regular file owned by the caller')
    finally:
        os.close(descriptor)
    return path


def connect(path):
    connection = sqlite3.connect(private_file(path), timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    connection.row_factory = sqlite3.Row
    try:
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        if version not in (0, 1, 2, SCHEMA_VERSION):
            raise ValueError(f'unsupported journal schema: {version}')
        connection.execute('PRAGMA foreign_keys=ON')
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA synchronous=FULL')
        with transaction(connection):
            version = connection.execute('PRAGMA user_version').fetchone()[0]
            if version == 0:
                if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
                    raise ValueError('unsupported journal schema: unversioned existing tables')
                for statement in SCHEMA:
                    connection.execute(statement)
            if version in (0, 1):
                for statement in OPERATIONAL_SCHEMA:
                    connection.execute(statement)
            if version in (0, 1, 2):
                for statement in MAIL_SCHEMA:
                    connection.execute(statement)
                connection.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
        return connection
    except BaseException:
        connection.close()
        raise
