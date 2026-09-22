"""Assignment lifecycle and result-file polling through the state API."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from . import packets
from .io import checked
from worker_state.types import iso_from

ASSIGNMENT_TIMEOUT = 300


def identity(snapshot: dict) -> str:
    goal = snapshot.get('goal', {})
    return f"{goal.get('goal_id') or 'unseen'}:{goal.get('attempt_id') or 0}"


def advance(client, worker: str, snapshot: dict, request_id: str) -> dict:
    body = {'worker': worker, 'action': 'advance',
            'expected_snapshot_version': snapshot['snapshot_version'],
            'idempotency_key': f'advance:{worker}:{identity(snapshot)}:{request_id}'}
    claim = checked(client.claim(body))
    checked(client.result(claim['action_id'], True))
    return claim


def event(client, worker: str, kind: str, planning: dict, now: float) -> dict:
    return checked(client.evidence({'event_id': f"{kind}-{planning['request_id']}",
        'worker': worker, 'kind': kind, 'source': 'api',
        'goal_id': planning['goal_id'], 'attempt_id': planning['attempt_id'],
        'source_timestamp': iso_from(now), 'payload': {
            'request_id': planning['request_id'], 'action_id': planning['action_id']}}))


def request(snapshot: dict, now: float) -> dict:
    goal = snapshot['goal']
    return {'request_id': f'r-{uuid4().hex}', 'woke_at': now,
            'goal_id': goal.get('goal_id'), 'attempt_id': goal.get('attempt_id'),
            'origin': identity(snapshot), 'status': 'prepared'}


def result(directory: Path, current: dict, now: float) -> dict:
    polled = packets.poll(directory, current)
    if 'packets' in polled:
        return {'action': 'ready', **polled}
    if now - current['woke_at'] >= ASSIGNMENT_TIMEOUT:
        return {'action': 'timeout', 'reason': polled.get('error', 'assignment_timeout')}
    return {'action': 'waiting', **polled}
