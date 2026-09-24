"""Pick a dispatch target using free-first burn when the work allows it.

Paid ClinePass is the reliability default for review/blocker. Free tier is
use-or-lose within 24h, so suitable packets go free first and fall back when
exhausted. Reliability never rides on free for judgment work.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import urllib.parse
import urllib.request

from . import cline_free

RELIABLE_KINDS = frozenset({'review', 'blocker'})
PACKET_USAGE_PATH = Path.home() / '.local/state/remote-agent/cline-free-packets.json'
PACKET_ATTEMPT = 'cline-free/cline-free/deepseek-v4.1-flash'


def suitable_for_free(item: dict) -> bool:
    kind = item.get('kind')
    if kind in RELIABLE_KINDS:
        return False
    if item.get('requires_astra_review'):
        return False
    return kind in {'implement', 'mechanical'}


def pick_assignee(item: dict, free_status: dict, *, preferred: str | None = None) -> dict:
    """Return {assignee, tier, reason} for one packet."""
    intended = preferred or item.get('assignee')
    if item.get('tier') == 'paid':
        return {'assignee': intended, 'tier': 'paid', 'reason': 'prior_free_refusal'}
    if not intended:
        return {'assignee': 'efficient', 'tier': 'paid', 'reason': 'default_mechanical'}
    if not suitable_for_free(item):
        return {'assignee': intended, 'tier': 'paid', 'reason': f'reliable_{item.get("kind")}'}
    free_ok = bool(free_status.get('ok')) and not free_status.get('exhausted')
    if free_ok and intended in {'flash', 'efficient'}:
        return {'assignee': 'flash', 'tier': 'free', 'reason': 'free_first',
                'paid_assignee': intended}
    return {'assignee': intended, 'tier': 'paid', 'reason': 'free_exhausted'}


def route_packet(item: dict, free_status: dict) -> dict:
    return {**item, **pick_assignee(item, free_status)}


def route_queue(packets: list[dict], free_status: dict) -> list[dict]:
    return [{**item, **pick_assignee(item, free_status, preferred=item.get('assignee'))}
            for item in packets]


def proxy_health(env: dict) -> dict | None:
    endpoint = str(env.get('SPECTRE_OMNI_ENDPOINT', 'http://127.0.0.1:8790/v1'))
    try:
        parsed = urllib.parse.urlsplit(endpoint)
    except ValueError:
        return None
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
            or not parsed.port or parsed.path != '/v1' or parsed.query or parsed.fragment
            or parsed.username or parsed.password):
        return None
    request = urllib.request.Request(f'{parsed.scheme}://{parsed.netloc}/omni/health')
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(request, timeout=2) as response:
            payload = json.loads(response.read(64 * 1024))
    except (OSError, ValueError):
        return None
    providers = payload.get('providers') if isinstance(payload, dict) else None
    if (not isinstance(payload, dict) or payload.get('ok') is not True
            or not isinstance(providers, list)
            or not {'cline-free', 'cline-paid'}.issubset(
                {item for item in providers if isinstance(item, str)})):
        return None
    stats = payload.get('stats')
    if not isinstance(stats, dict) or not isinstance(stats.get('attempts'), dict):
        return None
    return payload


def free_status_now(now: float, path: Path | None = None, env: dict | None = None) -> dict:
    health = proxy_health(os.environ if env is None else env)
    if health is None:
        return {'ok': False, 'configured': False, 'exhausted': False,
                'reason': 'proxy_unavailable'}
    target = path or PACKET_USAGE_PATH
    state = cline_free.load(target)
    stats = health.get('stats')
    attempts = stats.get('attempts') if isinstance(stats, dict) else None
    attempt = attempts.get(PACKET_ATTEMPT) if isinstance(attempts, dict) else None
    last_status = attempt.get('last_status') if isinstance(attempt, dict) else None
    if type(last_status) is int and last_status in {401, 403}:
        return {'ok': False, 'configured': True, 'exhausted': False,
                'reason': 'free_credentials_unavailable'}
    last_429 = attempt.get('last_429_at') if isinstance(attempt, dict) else None
    if (type(last_429) in {int, float} and 0 <= now - last_429 < cline_free.WINDOW_SEC
            and state.get('first_429_at') != last_429):
        state = cline_free.record(target, last_429, 429)
    return cline_free.status(state, now)
