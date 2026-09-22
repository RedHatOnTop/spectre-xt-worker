"""Top-level seat ownership and warm handoff.

One seat per project. Astra (codex) is primary; kimi_free is the standby that
opens only when every astra relay is dead. Handoff is a brief-based warm start,
never a cross-harness resume. Daily handoff cap stops flip-flop.
"""
from __future__ import annotations

from pathlib import Path
import time

from .io import locked, read_json, write_json, write_text

MAX_HANDOFFS_PER_DAY = 2
OWNER_ASTRA = 'codex'
OWNER_KIMI = 'kimi'


def seat_path(root: Path, worker: str = 'minecraft') -> Path:
    return Path(root) / 'toplevel' / worker / 'seat.json'


def brief_path(root: Path, worker: str = 'minecraft') -> Path:
    return Path(root) / 'toplevel' / worker / 'brief.md'


def default_state(worker: str = 'minecraft') -> dict:
    return {
        'worker': worker,
        'owner': OWNER_ASTRA,
        'session_id': None,
        'model': 'gpt-6-astra',
        'provider_chain': ['agentrouter', 'anyrouter'],
        'standby': {'owner': OWNER_KIMI, 'model': 'cline-free/kimi-k3', 'endpoint': 'http://127.0.0.1:8790/v1'},
        'handoff_count': 0,
        'handoff_day': '',
        'last_handoff_at': None,
        'history': [],
    }


def load(path: Path) -> dict:
    try:
        row = read_json(path)
    except (OSError, ValueError):
        row = {}
    if not isinstance(row, dict) or not row.get('owner'):
        row = default_state(str(row.get('worker') if isinstance(row, dict) else 'minecraft'))
    return row


def _day(now: float) -> str:
    return time.strftime('%Y-%m-%d', time.localtime(now))


def can_handoff(state: dict, now: float, cap: int = MAX_HANDOFFS_PER_DAY) -> tuple[bool, str]:
    if state.get('handoff_day') != _day(now):
        return True, 'new_day'
    if int(state.get('handoff_count') or 0) >= cap:
        return False, 'handoff_day_cap'
    return True, 'budget_ok'


def write_brief(path: Path, goal: str, decisions: list[str], outcomes: list[str]) -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        '# Top-level handoff brief',
        '',
        f'## Goal',
        goal.strip() or '(none)',
        '',
        '## Open decisions',
        *([f'- {item}' for item in decisions if item] or ['- (none)']),
        '',
        '## Recent outcomes',
        *([f'- {item}' for item in outcomes if item] or ['- (none)']),
        '',
        'Continue from the open decisions. Do not restart finished work.',
    ]
    write_text(path, '\n'.join(lines) + '\n')
    return {'ok': True, 'path': str(path)}


def prepare_handoff(state: dict, root: Path, now: float, *, to: str = OWNER_KIMI,
                    goal: str = '', decisions: list[str] | None = None,
                    outcomes: list[str] | None = None, cap: int = MAX_HANDOFFS_PER_DAY) -> dict:
    ok, reason = can_handoff(state, now, cap)
    if not ok:
        return {'ok': False, 'error': reason}
    if to not in {OWNER_KIMI, OWNER_ASTRA}:
        return {'ok': False, 'error': 'unknown_owner'}
    if to == state.get('owner'):
        return {'ok': False, 'error': 'already_owner'}
    brief = write_brief(brief_path(root, state.get('worker', 'minecraft')), goal,
                        decisions or [], outcomes or [])
    day = _day(now)
    count = int(state.get('handoff_count') or 0) + 1 if state.get('handoff_day') == day else 1
    history = [*state.get('history', []), {
        'at': now, 'from': state.get('owner'), 'to': to, 'brief': brief['path']}][-20:]
    model = 'cline-free/kimi-k3' if to == OWNER_KIMI else 'gpt-6-astra'
    chain = ['cline-free'] if to == OWNER_KIMI else ['agentrouter', 'anyrouter']
    updated = {
        **state,
        'owner': to,
        'model': model,
        'provider_chain': chain,
        'session_id': None,
        'handoff_count': count,
        'handoff_day': day,
        'last_handoff_at': now,
        'history': history,
    }
    return {'ok': True, 'seat': updated, 'brief': brief, 'reason': reason}


def commit(path: Path, updated: dict) -> dict:
    with locked(path.with_suffix('.lock')):
        write_json(path, updated)
    return {'ok': True, 'seat': updated}


def cold_start_prompt(state: dict, root: Path) -> str:
    brief = brief_path(root, state.get('worker', 'minecraft'))
    return (
        f'Read {brief} and continue the top-level session. '
        'You are the standby seat after an astra provider outage. '
        'Do not restart completed work; follow the open decisions in the brief.'
    )
