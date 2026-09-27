"""cline-free 24h use-or-lose tracker. Limits are unpublished; we accumulate evidence."""
from __future__ import annotations

from pathlib import Path

from .io import read_json, write_json

WINDOW_SEC = 24 * 3600
# First 429 is the only public signal of the hidden daily ceiling.
DEFAULT_PATH = Path.home() / '.local/state/remote-agent/cline-free-usage.json'


def load(path: Path | None = None) -> dict:
    target = path or DEFAULT_PATH
    try:
        row = read_json(target)
    except (OSError, ValueError):
        row = {}
    if not isinstance(row, dict):
        row = {}
    return {
        'model': row.get('model', 'cline-free/kimi-k3'),
        'window_start': float(row.get('window_start') or 0),
        'window_sec': int(row.get('window_sec') or WINDOW_SEC),
        'calls_today': int(row.get('calls_today') or 0),
        'tokens_in': int(row.get('tokens_in') or 0),
        'tokens_out': int(row.get('tokens_out') or 0),
        'first_429_at': row.get('first_429_at'),
        'last_status': row.get('last_status'),
        'history': [h for h in (row.get('history') or []) if isinstance(h, dict)][-50:],
    }


def _in_window(state: dict, now: float) -> bool:
    start = state.get('window_start') or 0
    return start and now - start < state.get('window_sec', WINDOW_SEC)


def record(path: Path | None, now: float, status: int, tokens_in: int = 0,
           tokens_out: int = 0) -> dict:
    state = load(path)
    if not _in_window(state, now):
        state = {**state, 'window_start': now, 'calls_today': 0,
                 'tokens_in': 0, 'tokens_out': 0, 'first_429_at': None}
    if status == 429:
        if not state.get('first_429_at'):
            state = {**state, 'first_429_at': now}
    elif 200 <= status < 300:
        state = {**state, 'calls_today': state['calls_today'] + 1,
                 'tokens_in': state['tokens_in'] + max(0, tokens_in),
                 'tokens_out': state['tokens_out'] + max(0, tokens_out)}
    entry = {'at': now, 'status': status, 'call': state['calls_today']}
    state = {**state, 'last_status': status,
             'history': [*state['history'], entry][-50:]}
    write_json(path or DEFAULT_PATH, state)
    return state


def reset_at(state: dict) -> float | None:
    """Best-effort end of the current 24h window."""
    start = state.get('window_start') or 0
    if not start:
        return None
    return start + state.get('window_sec', WINDOW_SEC)


def status(state: dict, now: float) -> dict:
    """What the free-tier router needs: ok / exhausted / est_limit hint."""
    if state.get('first_429_at') and _in_window(state, now):
        return {'ok': False, 'configured': True, 'exhausted': True,
                'calls_today': state['calls_today'],
                'est_limit': state['calls_today'],
                'reset_at': reset_at(state), 'status': 429}
    if not _in_window(state, now):
        state = {**state, 'calls_today': 0, 'first_429_at': None}
    est = state['calls_today'] + 1 if not state.get('first_429_at') else state['calls_today']
    return {'ok': True, 'configured': True, 'exhausted': False,
            'calls_today': state['calls_today'],
            'est_limit': state.get('first_429_at') and state['calls_today'] or None,
            'reset_at': reset_at(state), 'status': state.get('last_status'),
            'hint_remaining': 'unknown' if not state.get('first_429_at') else max(
                0, (state.get('est_limit') or state['calls_today']) - state['calls_today'])}
