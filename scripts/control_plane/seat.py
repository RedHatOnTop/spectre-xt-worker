"""Top-level seat ownership, the seat ladder, and warm handoff.

One seat per project. The routing policy is the ordered ladder in `SEAT_LADDER`:
each rank is a seat, and a seat is a `(harness, provider, model, wire_api)`
tuple. Every model is served by its own harness, so a seat can only be entered
once its harness is running, and falling back means moving down the ladder
rather than rotating providers inside one harness.

Handoff inside one harness is a native resume. Crossing harnesses is a warm
start from the brief, because native transcripts are not portable between
harnesses. The daily cap stops flip-flop.

`provider_chain` in the seat state is the pre-ladder view and is superseded by
the ladder; it is still written so existing readers keep working.
"""
from __future__ import annotations

from pathlib import Path
import time

from .io import locked, read_json, write_json, write_text

MAX_HANDOFFS_PER_DAY = 2
OWNER_ASTRA = 'codex'
OWNER_KIMI = 'kimi'
OWNER_CLAUDE = 'claude'
OWNERS = (OWNER_CLAUDE, OWNER_ASTRA, OWNER_KIMI)

# Every wire API a seat may be probed or driven through.
WIRE_APIS = frozenset({'messages', 'responses', 'chat'})

# Operator-locked routing policy, best seat first. `harness` names the client
# that must run for that model; `wire_api` is the surface that harness uses.
# Rank 1 is Claude Opus 5.5 on anyrouter's Anthropic surface, which additionally
# requires the 1M-context beta. Ranks 2 and 4 share the Codex harness, so moving
# between them is a native resume. This constant is validated by
# tests/test_control_plane_seat.py rather than at import time: a bad edit must
# fail the gate, not take the control plane down at import.
SEAT_LADDER = (
    {'rank': 1, 'harness': OWNER_CLAUDE, 'provider': 'anyrouter',
     'model': 'claude-opus-5-5', 'wire_api': 'messages'},
    {'rank': 2, 'harness': OWNER_ASTRA, 'provider': 'agentrouter',
     'model': 'gpt-6-astra', 'wire_api': 'responses'},
    {'rank': 3, 'harness': OWNER_KIMI, 'provider': 'cline-free',
     'model': 'cline-free/kimi-k3', 'wire_api': 'chat'},
    {'rank': 4, 'harness': OWNER_ASTRA, 'provider': 'openai',
     'model': 'gpt-6-astra', 'wire_api': 'responses'},
)

# Process role of each owner's planner, as inventory.model() and
# scripts/dispatch-process.mjs name it. The three tables must agree.
PLANNER_ROLES = {OWNER_ASTRA: 'astra', OWNER_CLAUDE: 'claude', OWNER_KIMI: 'kimi'}
# anyrouter's edge answers 524 at ~301 s per request and prefill costs ~1 s per
# 1k tokens, so a Claude plan needs more than one edge window.
PLAN_TIMEOUTS = {OWNER_CLAUDE: 900}


def planner_role(owner: str) -> str | None:
    return PLANNER_ROLES.get(owner)


def ladder() -> list[dict]:
    """The routing policy as copies, best seat first."""
    return [dict(row) for row in SEAT_LADDER]


def validate_ladder(rows) -> None:
    """Raise ValueError unless `rows` is an ordered, usable seat ladder."""
    if not isinstance(rows, (list, tuple)) or not rows:
        raise ValueError('ladder: expected a non-empty sequence')
    for index, row in enumerate(rows):
        where = f'ladder[{index}]'
        if not isinstance(row, dict):
            raise ValueError(f'{where}: expected an object')
        if type(row.get('rank')) is not int or row['rank'] != index + 1:
            raise ValueError(f'{where}: rank must be {index + 1}')
        for field in ('harness', 'provider', 'model'):
            value = row.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f'{where}: {field} must be a non-empty string')
        if row['harness'] not in OWNERS:
            raise ValueError(f'{where}: unknown harness {row["harness"]!r}')
        if row.get('wire_api') not in WIRE_APIS:
            raise ValueError(f'{where}: unknown wire_api {row.get("wire_api")!r}')


def seat_for_rank(rank: int) -> dict:
    for row in SEAT_LADDER:
        if row['rank'] == rank:
            return dict(row)
    raise ValueError(f'unknown seat rank {rank}')


def seats_for_owner(owner: str) -> list[dict]:
    """Every seat that runs under `owner`, best rank first."""
    return [dict(row) for row in SEAT_LADDER if row['harness'] == owner]


def owner_rank(owner: str) -> int:
    """Best (lowest) rank owned by `owner`; raises when it has no seat."""
    seats = seats_for_owner(owner)
    if not seats:
        raise ValueError(f'owner {owner!r} has no seat')
    return seats[0]['rank']


def seat_path(root: Path, worker: str = 'minecraft') -> Path:
    return Path(root) / 'toplevel' / worker / 'seat.json'


def brief_path(root: Path, worker: str = 'minecraft') -> Path:
    return Path(root) / 'toplevel' / worker / 'brief.md'


def default_state(worker: str = 'minecraft') -> dict:
    return {
        'worker': worker,
        'owner': OWNER_ASTRA,
        'session_id': None,
        'terminal': None,
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
        path.lstat()
    except FileNotFoundError:
        return default_state(path.parent.name)
    if path.is_symlink():
        raise ValueError(f'{path.name}: symlink seat state is not allowed')
    row = read_json(path)
    if (row.get('worker') != path.parent.name or
            row.get('owner') not in OWNERS or
            type(row.get('handoff_count')) is not int or row['handoff_count'] < 0 or
            not isinstance(row.get('history'), list) or
            not all(isinstance(item, dict) for item in row['history']) or
            not isinstance(row.get('handoff_day'), str) or
            not isinstance(row.get('standby'), dict) or
            not isinstance(row['standby'].get('endpoint'), str) or
            row.get('session_id') is not None and not isinstance(row['session_id'], str) or
            row.get('terminal') is not None and not isinstance(row['terminal'], str)):
        raise ValueError(f'{path.name}: invalid seat state')
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
    if to not in OWNERS:
        return {'ok': False, 'error': 'unknown_owner'}
    if to == state.get('owner'):
        return {'ok': False, 'error': 'already_owner'}
    brief = write_brief(brief_path(root, state.get('worker', 'minecraft')), goal,
                        decisions or [], outcomes or [])
    return transition(state, now, to=to, brief=brief['path'], cap=cap)


def transition(state: dict, now: float, *, to: str, brief: str,
               cap: int = MAX_HANDOFFS_PER_DAY) -> dict:
    ok, reason = can_handoff(state, now, cap)
    if not ok:
        return {'ok': False, 'error': reason}
    if to not in OWNERS:
        return {'ok': False, 'error': 'unknown_owner'}
    if to == state.get('owner'):
        return {'ok': False, 'error': 'already_owner'}
    day = _day(now)
    count = int(state.get('handoff_count') or 0) + 1 if state.get('handoff_day') == day else 1
    history = [*state.get('history', []), {
        'at': now, 'from': state.get('owner'), 'to': to, 'brief': brief}][-20:]
    best = seats_for_owner(to)[0]
    chain = ['agentrouter', 'anyrouter'] if to == OWNER_ASTRA else [best['provider']]
    updated = {
        **state,
        'owner': to,
        'model': best['model'],
        'provider_chain': chain,
        'session_id': None,
        'terminal': None,
        'handoff_count': count,
        'handoff_day': day,
        'last_handoff_at': now,
        'history': history,
    }
    return {'ok': True, 'seat': updated, 'brief': {'ok': True, 'path': brief},
            'reason': reason}


def commit(path: Path, updated: dict) -> dict:
    with locked(path.with_suffix('.lock')):
        write_json(path, updated)
    return {'ok': True, 'seat': updated}


def commit_if_current(path: Path, expected: dict, updated: dict) -> dict:
    with locked(path.with_suffix('.lock')):
        if load(path) != expected:
            return {'ok': False, 'error': 'seat_changed'}
        write_json(path, updated)
    return {'ok': True, 'seat': updated}


def cold_start_prompt(state: dict, root: Path) -> str:
    brief = brief_path(root, state.get('worker', 'minecraft'))
    return (
        f'Read {brief} and continue the top-level session. '
        'You are the standby seat after an astra provider outage. '
        'Do not restart completed work; follow the open decisions in the brief.'
    )
