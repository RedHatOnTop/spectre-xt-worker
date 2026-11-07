"""Page offload completion facts into review context without native settlement."""
from pathlib import Path

from . import execution, offload_artifact, offload_cleanup_source, offload_receipt
from .execution_source import names, position
from .model import identifier
from .sources import READ_LIMIT, SourcePage


def event(root, record):
    return {'source': 'offload-execution', 'event_id': 'offload-' + record['intent_id'],
            'content': {**record['binding'], 'job_id': record['job_id'], 'admission': 'review_only',
                        'execution_only': True, 'returncode': record['returncode'],
                        'finished_at': record['finished_at'], 'spawned': record['spawned'],
                        'remote_observation': record['remote_observation'],
                        'remote_result': record['remote_result'], 'remote_error': record.get('remote_error'),
                        'error': record.get('error'), 'stdout_path': str(root / record['job_id'] / 'stdout.log'),
                        'stderr_path': str(root / record['job_id'] / 'stderr.log'),
                        'native_outcome': 'Inspect the exact native Dispatch; OS exit is not Task success.'}}


def reconciliation_event(root, record):
    proof = record['reconciliation']
    original = event(root, record)
    return {**original, 'source': 'offload-reconciliation', 'event_id': 'offload-reconciled-' + record['intent_id'],
            'content': {**original['content'], 'remote_observation': 'exited',
                        'remote_result': proof['remote_result'], 'reconciliation': proof,
                        'remote_error': None, 'original_wrapper_exit_unchanged': True}}


def completion_events(root, record):
    original = ((event(root, record), reconciliation_event(root, record))
                if record.get('reconciliation') else (event(root, record),))
    artifact = offload_artifact.observe(root, record)
    returned = (*original, offload_artifact.event(record, artifact)) if artifact is not None else original
    return (*returned, *offload_cleanup_source.events(root, record))


def page(root, run_id, cursor, limit=READ_LIMIT):
    identifier(run_id)
    current = position(cursor)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= READ_LIMIT:
        raise ValueError('offload source limit must be between 1 and 100')
    root = Path(root).absolute()
    if root != root.resolve():
        raise ValueError('offload source cannot contain symlinks')
    if not root.exists():
        return SourcePage((), {'last_id': None, 'upper_id': None})
    execution.private_directory(root)
    upper, selected = names(root, current, limit)
    records = tuple(offload_receipt.inspect(root, name) for name in selected[:limit])
    events = tuple(value for record in records if record.get('returncode') is not None
                   and record['binding']['run_id'] == run_id
                   for value in completion_events(root, record))
    next_cursor = {'last_id': selected[limit - 1], 'upper_id': upper} if len(selected) > limit else {
        'last_id': None, 'upper_id': None}
    return SourcePage(events, next_cursor)
