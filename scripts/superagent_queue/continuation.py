"""Prove obsolete automatic intent without cancelling inputs or native work."""
import hashlib
import math

from orca_runtime import observe
from .disposition import validate
from .model import encoded
from .target import owner

REVIEW_SOURCES = frozenset(('orca-inbox', 'orca-gates', 'discord-intake', 'dsh-execution',
                           'offload-execution', 'offload-reconciliation', 'offload-artifact',
                           'offload-cleanup', 'offload-resource'))


def provider_wait(disposition, now):
    return disposition.get('state') == 'waiting_provider' and now < disposition['retry_at']


def held(target, data, disposition, now):
    if any(w.get('runId') == target['run_id'] and (
            observe.active(w) or w.get('dispatchStatus') == 'dispatched') for w in data.get('workers', [])):
        return True
    if disposition.get('state') in ('waiting_job', 'waiting_approval', 'finished'):
        return True
    return provider_wait(disposition, now)


def turn(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


def digest(claim, content):
    return hashlib.sha256(encoded({'content': content, 'key': claim.key, 'kind': claim.kind,
                                  'lane': claim.lane, 'payload': claim.payload}).encode()).hexdigest()


def original(claim, event):
    if not event or event['source'] != 'continuity' or event['event_id'] != claim.key or claim.generation != 1:
        return None
    content, payload = event['content'], claim.payload
    prefix = f'[spectre-delivery:{claim.key}] '
    if (set(content) != {'turn_ended_at', 'disposition'} or not turn(content['turn_ended_at'])
            or not isinstance(content['disposition'], dict) or not payload['text'].startswith(prefix)):
        return None
    target = {**payload, 'text': payload['text'][len(prefix):]}
    identity = {'target': target, 'turn_ended_at': content['turn_ended_at'], 'disposition': content['disposition']}
    key = 'resume-' + hashlib.sha256(encoded(identity).encode()).hexdigest()
    if key != claim.key or digest(claim, content) != event['identity'] or claim.lane != target['run_id']:
        return None
    if content['disposition']:
        validate({target['run_id']: content['disposition']})
    return content


def reviewed_context(claim, event):
    if not event or event['source'] not in REVIEW_SOURCES or claim.generation != 1:
        return False
    key = 'context-' + hashlib.sha256(
        f'{event["source"]}:{event["event_id"]}:{claim.payload["run_id"]}'.encode()).hexdigest()
    return key == claim.key and claim.lane == claim.payload['run_id'] and digest(claim, event['content']) == event['identity']


def eligible(claim, before, data, decision, now):
    current = owner(claim.payload, data)
    since = current.state_started_at
    return (current.idle and turn(since) and since == before['turn_ended_at'] and since / 1000 <= now
            and decision == before['disposition'] and not held(claim.payload, data, decision, now))


def proof(claim, event, data, disposition, now):
    before = original(claim, event)
    if before is None:
        return None
    current = owner(claim.payload, data)
    since = current.state_started_at
    if not current.idle or not turn(since) or since < before['turn_ended_at'] or since / 1000 > now:
        return None
    after = {'turn_ended_at': since, 'disposition': disposition}
    if after == before and not held(claim.payload, data, disposition, now):
        return None
    blockers = [{'dispatch_id': row.get('dispatchId'), 'dispatch_status': row.get('dispatchStatus'),
                 'terminal_state': row.get('terminalState')}
                for row in data.get('workers', []) if row.get('runId') == claim.lane
                and (observe.active(row) or row.get('dispatchStatus') == 'dispatched')]
    return {'source': 'continuation_supersession', 'delivery_sent': False,
            'event_identity': event['identity'], 'generation': claim.generation,
            'original': before, 'current': after, 'blocking_dispatches': blockers,
            'owner': {'run_id': claim.lane, 'terminal_handle': claim.payload['terminal_handle'],
                      'worktree_id': claim.payload['worktree_id'], 'pane_key': current.pane_key,
                      'incarnation_id': claim.payload['incarnation_id']}}
