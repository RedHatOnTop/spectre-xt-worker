"""Pick a dispatch target using free-first burn when the work allows it.

Paid ClinePass is the reliability default for review/blocker. Free tier is
use-or-lose within 24h, so suitable packets go free first and fall back when
exhausted. Reliability never rides on free for judgment work.
"""
from __future__ import annotations

from . import cline_free

RELIABLE_KINDS = frozenset({'review', 'blocker'})


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
    if not intended:
        return {'assignee': 'efficient', 'tier': 'paid', 'reason': 'default_mechanical'}
    if not suitable_for_free(item):
        return {'assignee': intended, 'tier': 'paid', 'reason': f'reliable_{item.get("kind")}'}
    free_ok = bool(free_status.get('ok')) and not free_status.get('exhausted')
    if free_ok:
        return {'assignee': intended, 'tier': 'free', 'reason': 'free_first'}
    return {'assignee': intended, 'tier': 'paid', 'reason': 'free_exhausted'}


def route_queue(packets: list[dict], free_status: dict) -> list[dict]:
    return [{**item, **pick_assignee(item, free_status, preferred=item.get('assignee'))}
            for item in packets]


def free_status_now(now: float, path=None) -> dict:
    return cline_free.status(cline_free.load(path), now)
