"""Validated immutable effect claims and serialized operational inputs."""
from dataclasses import dataclass
import json
import math
import re
import time

MAX_JSON_BYTES = 64 * 1024
INITIAL_DELAY = 2
MAX_DELAY = 60
KINDS = frozenset(('wake', 'delivery', 'notice', 'reply'))
STATUSES = ('pending', 'running', 'reconciling', 'attention', 'delivered')
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}')


def identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        raise ValueError('journal identifiers must be bounded nonempty tokens')
    return value


def timestamp(value):
    stamp = time.time() if value is None else value
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)) or not math.isfinite(stamp) or stamp < 0:
        raise ValueError('journal time must be nonnegative and finite')
    return stamp


def encoded(value):
    if not isinstance(value, dict):
        raise ValueError('journal payload must be a JSON object')
    try:
        text = json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError('journal payload must be finite JSON') from exc
    if len(text.encode('utf-8')) > MAX_JSON_BYTES:
        raise ValueError('journal payload exceeds the byte limit')
    return text


def failure_text(value):
    if not isinstance(value, str) or not value or len(value.encode('utf-8')) > MAX_JSON_BYTES:
        raise ValueError('journal failure must be bounded nonempty text')
    return value


@dataclass(frozen=True)
class Claim:
    key: str
    kind: str
    lane: str
    generation: int
    token: str
    owner: str
    payload_json: str

    @property
    def payload(self):
        return json.loads(self.payload_json)
