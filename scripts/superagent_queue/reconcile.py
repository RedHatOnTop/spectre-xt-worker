"""Confirm marked delivery from native identity without altering Orca receipts."""
import math
import re

from .model import timestamp
from .target import owner, prompt_receipt

MARKER = re.compile(r'^\[spectre-delivery:[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}\] ')
NATIVE_PROMPT_LIMIT = 200


def started(target, receipt, inventory, claimed_at, now=None):
    marker = MARKER.match(target['text'])
    if not marker or receipt.get('ok') is not True:
        return None
    prompt = prompt_receipt(receipt)
    result = receipt.get('result')
    sent = result.get('send') if isinstance(result, dict) else None
    if not isinstance(sent, dict):
        return None
    if (sent.get('handle') != target['terminal_handle'] or sent.get('accepted') is not True
            or prompt.get('processIncarnation') != target['incarnation_id']
            or not isinstance(prompt.get('requestId'), str) or not prompt['requestId']):
        return None
    try:
        current = owner(target, inventory)
    except (ValueError, RuntimeError):
        return None
    since = current.state_started_at
    completed = (current.state == 'done' and current.interrupted is False
                 and isinstance(current.last_assistant_message, str) and bool(current.last_assistant_message.strip()))
    if ((current.state != 'working' and not completed) or isinstance(since, bool)
            or not isinstance(since, (int, float)) or not math.isfinite(since)
            or not timestamp(claimed_at) <= since / 1000 <= timestamp(now)):
        return None
    expected = target['text'][:NATIVE_PROMPT_LIMIT]
    if current.prompt != expected:
        return None
    return {'source': 'native_agent_snapshot', 'request_id': prompt['requestId'],
            'terminal_handle': target['terminal_handle'], 'incarnation_id': target['incarnation_id'],
            'worktree_id': target['worktree_id'], 'pane_key': current.pane_key, 'state_started_at': since,
            'prompt_prefix': expected, 'state': current.state, 'native_stages': prompt.get('stages', []),
            'delivery_only': True}
