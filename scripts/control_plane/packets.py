"""Strict next-goal and planner packet contracts."""
from __future__ import annotations

import json
from pathlib import Path
import re

GOAL_MAX = 600
PACKET_MAX = 4
FILE_MAX = 64 * 1024
IDENT = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_-]{0,95}$')


def valid_goal(value) -> bool:
    return isinstance(value, str) and 0 < len(value.strip()) <= GOAL_MAX and '\x00' not in value


def next_goal(cwd: str) -> str | None:
    if not cwd or not Path(cwd).is_absolute():
        return None
    path = Path(cwd) / 'next_goal.json'
    try:
        if path.stat().st_size > FILE_MAX:
            return None
        payload = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get('blocked') is True:
        return None
    value = payload.get('goal')
    return value.strip() if valid_goal(value) else None


def validate(payload: dict, request_id: str) -> list[dict]:
    if not isinstance(payload, dict):
        raise ValueError('packet result must be an object')
    if set(payload) != {'request_id', 'worker', 'wake_reason', 'packets'}:
        raise ValueError('unexpected result fields')
    if payload.get('request_id') != request_id or not IDENT.fullmatch(request_id):
        raise ValueError('packet request_id mismatch')
    if payload.get('worker') != 'minecraft':
        raise ValueError('planner is minecraft-only')
    if payload.get('wake_reason') not in {'completed', 'failed', 'hard_decision'}:
        raise ValueError('invalid wake_reason')
    items = payload.get('packets')
    if not isinstance(items, list) or not 1 <= len(items) <= PACKET_MAX:
        raise ValueError('expected 1 to 4 packets')
    for item in items:
        validate_packet(item)
    if len({item['id'] for item in items}) != len(items):
        raise ValueError('duplicate packet id')
    return [dict(item) for item in items]


def validate_packet(item: dict) -> None:
    if not isinstance(item, dict) or not isinstance(item.get('id'), str) or not IDENT.fullmatch(item['id']):
        raise ValueError('invalid packet id')
    if set(item) != {'id', 'assignee', 'kind', 'goal', 'acceptance', 'requires_astra_review'}:
        raise ValueError('unexpected packet fields')
    if item.get('assignee') not in {'flash', 'mimo', 'efficient'}:
        raise ValueError('invalid packet assignee')
    if item.get('kind') not in {'implement', 'mechanical', 'review', 'blocker'}:
        raise ValueError('invalid packet kind')
    if item['assignee'] == 'efficient' and item['kind'] != 'mechanical':
        raise ValueError('Efficient accepts mechanical packets only')
    if item['assignee'] == 'flash' and item['kind'] not in {'implement', 'mechanical', 'blocker'}:
        raise ValueError('Flash accepts implement, mechanical, or blocker')
    if item['assignee'] == 'mimo' and item['kind'] == 'mechanical':
        raise ValueError('Mimo does not take mechanical; use flash or efficient')
    if not valid_goal(item.get('goal')):
        raise ValueError('invalid packet goal')
    acceptance = item.get('acceptance')
    if not isinstance(acceptance, list) or not 1 <= len(acceptance) <= 8:
        raise ValueError('invalid packet acceptance')
    if any(not valid_goal(text) for text in acceptance):
        raise ValueError('invalid acceptance text')
    if type(item.get('requires_astra_review')) is not bool:
        raise ValueError('requires_astra_review must be boolean')


def poll(directory: Path, planning: dict) -> dict:
    ident = planning['request_id']
    if not IDENT.fullmatch(ident):
        raise ValueError('invalid stored request_id')
    path = directory / f'{ident}.json'
    try:
        stat = path.stat()
        stamp = [stat.st_size, stat.st_mtime_ns]
        if stat.st_size > FILE_MAX or path.is_symlink():
            return {'error': 'unsafe packet file'}
        if planning.get('file_stamp') != stamp:
            return {'file_stamp': stamp}
        payload = json.loads(path.read_text(encoding='utf-8'))
        return {'packets': validate(payload, ident), 'file_stamp': stamp}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        return {'error': str(exc)}
