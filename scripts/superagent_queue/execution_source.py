"""Page immutable OS execution exits into the existing review-only event journal."""
import heapq
import os
from pathlib import Path

from . import execution
from .model import identifier
from .sources import READ_LIMIT, SourcePage


def position(cursor):
    value = {'last_id': None, 'upper_id': None} if cursor is None else cursor
    if (not isinstance(value, dict) or not {'last_id', 'upper_id'} <= set(value)
            or set(value) - {'last_id', 'upper_id', 'revision'}):
        raise ValueError('execution cursor requires exact last and upper identifiers')
    for key in ('last_id', 'upper_id'):
        if value[key] is not None:
            identifier(value[key])
    if ((value['last_id'] is None) != (value['upper_id'] is None)
            or value['last_id'] is not None and value['last_id'] > value['upper_id']):
        raise ValueError('execution cursor is outside its fixed sweep boundary')
    return value


def names(root, cursor, limit):
    with os.scandir(root) as entries:
        upper = cursor['upper_id'] or max((entry.name for entry in entries), default=None)
    with os.scandir(root) as entries:
        return upper, heapq.nsmallest(limit + 1, (entry.name for entry in entries
            if (cursor['last_id'] is None or entry.name > cursor['last_id']) and entry.name <= upper))


def event(record):
    binding = record['binding']
    return {'source': 'dsh-execution', 'event_id': 'exit-' + record['intent_id'],
            'content': {**binding, 'admission': 'review_only', 'execution_only': True,
                        'returncode': record['returncode'], 'finished_at': record['finished_at'],
                        'spawned': record['spawned'], 'error': record.get('error'),
                        'native_outcome': 'Inspect the exact native Dispatch; OS exit is not Task success.'}}


def page(root, run_id, cursor, limit=READ_LIMIT):
    identifier(run_id)
    current = position(cursor)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= READ_LIMIT:
        raise ValueError('execution source limit must be between 1 and 100')
    root = Path(root).absolute()
    if root != root.resolve():
        raise ValueError('execution source cannot contain symlinks')
    if not root.exists():
        return SourcePage((), {'last_id': None, 'upper_id': None})
    execution.private_directory(root)
    upper, selected = names(root, current, limit)
    records = tuple(execution.inspect(root, name) for name in selected[:limit])
    events = tuple(event(record) for record in records if record.get('returncode') is not None
                   and record['binding']['run_id'] == run_id)
    next_cursor = {'last_id': selected[limit - 1], 'upper_id': upper} if len(selected) > limit else {
        'last_id': None, 'upper_id': None}
    return SourcePage(events, next_cursor)
