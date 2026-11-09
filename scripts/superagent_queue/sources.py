"""Read existing intake and native events as context, never execution permission."""
import hashlib
from contextlib import closing
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
import stat

from orca_runtime import observe
from .model import encoded, identifier

INTAKE_FIELDS = frozenset(('id', 'message_id', 'state', 'kind', 'title', 'summary', 'accepted_by', 'updated_at'))
READ_LIMIT = 100
NATIVE_FIELDS = ('id', 'type', 'subject', 'body', 'payload', 'sender_handle')


def native_message(value):
    if not isinstance(value, dict):
        raise ValueError('native message requires a bounded original object')
    identifier(value.get('id'))
    return {key: value.get(key) for key in NATIVE_FIELDS}


@dataclass(frozen=True)
class SourcePage:
    events: tuple
    cursor: dict


def intake_connection(path):
    path = Path(path).absolute()
    if path != path.resolve():
        raise ValueError('intake ledger cannot be a symlink')
    entry = path.stat()
    if (not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid()
            or stat.S_IMODE(entry.st_mode) & 0o077):
        raise ValueError('intake ledger must be a private regular file owned by the caller')
    return sqlite3.connect(f'{path.as_uri()}?mode=ro', uri=True)


def intake_page(path, cursor):
    position = {'last_id': None, 'upper_id': None} if cursor is None else cursor
    if (not isinstance(position, dict) or not {'last_id', 'upper_id'} <= set(position)
            or set(position) - {'last_id', 'upper_id', 'revision'}):
        raise ValueError('intake cursor requires exact last and upper identifiers')
    for key in ('last_id', 'upper_id'):
        if position[key] is not None:
            identifier(position[key])
    if ((position['last_id'] is None) != (position['upper_id'] is None)
            or position['last_id'] is not None and position['last_id'] > position['upper_id']):
        raise ValueError('intake cursor is outside its fixed sweep boundary')
    with closing(intake_connection(path)) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute('BEGIN')
        columns = {row[1] for row in connection.execute('PRAGMA table_info(todos)')}
        if not INTAKE_FIELDS <= columns:
            raise ValueError('incompatible existing intake schema')
        upper = position['upper_id'] or connection.execute('SELECT MAX(id) FROM todos').fetchone()[0]
        rows = connection.execute('''SELECT id,message_id,state,kind,title,summary,accepted_by,updated_at
            FROM todos WHERE (? IS NULL OR id>?) AND id<=? ORDER BY id LIMIT ?''',
            (position['last_id'], position['last_id'], upper, READ_LIMIT + 1)).fetchall()
    page = rows[:READ_LIMIT]
    events = tuple(intake_event(row) for row in page if row['state'] in ('queued', 'planned', 'deferred'))
    next_cursor = {'last_id': page[-1]['id'], 'upper_id': upper} if len(rows) > READ_LIMIT else {
        'last_id': None, 'upper_id': None}
    return SourcePage(events, next_cursor)


def intake(path):
    return list(intake_page(path, None).events)


def intake_event(row):
    content = {'todo_id': row['id'], 'message_id': row['message_id'], 'state': row['state'],
               'kind': row['kind'], 'title': row['title'], 'summary': row['summary'],
               'completion_reviewer': row['accepted_by'], 'admission': 'review_only', 'updated_at': row['updated_at']}
    key = 'todo-' + hashlib.sha256(encoded(content).encode()).hexdigest()
    return {'source': 'discord-intake', 'event_id': key, 'content': content}


def inbox(cli, target):
    result = observe.query(cli, ['orchestration', 'check', '--terminal', target['terminal_handle'],
                                 '--run', target['run_id'], '--peek'])
    messages = result.get('messages')
    if result.get('runId') != target['run_id'] or not isinstance(messages, list):
        raise ValueError('native inbox scope or schema cannot be confirmed')
    original = [native_message(row) for row in messages]
    return [{'source': 'orca-inbox', 'event_id': row['id'], 'content': row} for row in original]


def gates(cli, target, dispositions):
    expected = dispositions.get(target['run_id'], {}).get('gate_ids', [])
    if not expected:
        return []
    result = observe.query(cli, ['orchestration', 'gate-list', '--run', target['run_id']])
    rows = result.get('gates')
    if result.get('runId') != target['run_id'] or not isinstance(rows, list):
        raise ValueError('native gate scope or schema cannot be confirmed')
    return [{'source': 'orca-gates', 'event_id': identifier(row['id']) + '-resolved',
             'content': {key: row.get(key) for key in ('id', 'status', 'resolution', 'resolved_at')}}
            for row in rows if row.get('id') in expected and row.get('status') == 'resolved']
