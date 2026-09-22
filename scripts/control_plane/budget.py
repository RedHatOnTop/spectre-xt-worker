"""Persistent dispatch limits, independent of model cost."""
from __future__ import annotations

import hashlib

DAY = 86400
SPACING = 60
WORKER_CAP = 96
GLOBAL_CAP = 256
PLUS_WINDOW = 5 * 3600


def fingerprint(text: str) -> str:
    return hashlib.sha256(' '.join(text.split()).encode()).hexdigest()


def refusal(state: dict, worker: str, text: str, now: float) -> str | None:
    history = [row for row in state.get('dispatches', []) if row['at'] > now - DAY]
    own = [row for row in history if row['worker'] == worker]
    last = state.get('workers', {}).get(worker, {}).get('last_fingerprint')
    if last == fingerprint(text):
        return 'repeated_goal'
    if own and now - max(row['at'] for row in own) < SPACING:
        return 'dispatch_spacing'
    if len(own) >= WORKER_CAP:
        return 'worker_daily_cap'
    if len(history) >= GLOBAL_CAP:
        return 'global_daily_cap'
    return None


def reserve(state: dict, worker: str, text: str, now: float) -> dict:
    history = [row for row in state.get('dispatches', []) if row['at'] > now - DAY]
    workers = state.get('workers', {})
    return {**state, 'dispatches': [*history, {'worker': worker, 'at': now}],
            'workers': {**workers, worker: {**workers.get(worker, {}),
                         'last_fingerprint': fingerprint(text)}}}


def planner_refusal(state: dict, provider: dict, now: float, critical=False) -> str | None:
    if not provider.get('ok') or now - provider.get('checked_at', 0) > 600:
        return 'provider_health_stale'
    if provider.get('cooldown_until', 0) > now:
        return 'provider_cooldown'
    if provider.get('id') != 'openai':
        return None
    recent = [row for row in state.get('plans', []) if row['at'] > now - PLUS_WINDOW
              and row['provider'] == 'openai']
    if critical:
        return 'plus_critical_cap' if any(row.get('critical') for row in recent) else None
    return 'plus_normal_cap' if len([row for row in recent if not row.get('critical')]) >= 3 else None
