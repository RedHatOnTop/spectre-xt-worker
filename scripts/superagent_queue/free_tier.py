"""Free-first burn policy for native worker executions.

Paid ClinePass is the reliability default for review and blocker packets. The
free tier is use-or-lose, so packets that declare mechanical or implement work
go free first and fall back to paid with a recorded reason. Reliability never
rides on free for judgment work, and an unprovisioned free route is a visible
reason rather than a silent paid launch.
"""
import json
import os
from pathlib import Path
import time

KINDS = frozenset(('implement', 'mechanical', 'review', 'blocker'))
RELIABLE_KINDS = frozenset(('review', 'blocker'))
MARKER = 'kind:'
LEDGER = 'free-tier-decisions.jsonl'
REASONS = frozenset((
    'free_first', 'default_unknown_kind', 'reliable_review', 'reliable_blocker',
    'free_unprovisioned', 'free_key_missing', 'free_profile_unsafe', 'free_key_unsafe',
))


def state_root():
    return Path(os.environ.get('SPECTRE_FREE_TIER_STATE',
                               Path.home() / '.local/state/remote-agent'))


def home(root=None):
    return Path(root) if root is not None else Path.home() / '.local/share/fullmoon-dsh-free'


def key_file(root=None):
    return Path(root) if root is not None else Path.home() / '.config/omni-proxy/client_key'


def kind_of(text):
    """Read the coordinator's exact `Kind: <kind>` line; absence is unknown."""
    if not isinstance(text, str):
        return None
    for line in text.splitlines()[:10]:
        candidate = line.strip()
        if not candidate.lower().startswith(MARKER):
            continue
        value = candidate[len(MARKER):].strip().lower()
        return value if value in KINDS else None
    return None


def private_file(path):
    path = Path(path)
    try:
        entry = path.lstat()
    except OSError:
        return False
    return (not path.is_symlink() and path.is_file() and entry.st_uid == os.getuid()
            and entry.st_mode & 0o077 == 0 and entry.st_size > 0)


def readiness(home_path=None, key_path=None):
    """Whether the free route is provisioned; a reason is always present."""
    profile = home(home_path) / 'settings.yaml'
    credential = key_file(key_path)
    if not private_file(profile):
        return {'ok': False, 'reason': 'free_unprovisioned', 'profile': str(profile)}
    if not private_file(credential):
        return {'ok': False, 'reason': 'free_key_missing', 'key': str(credential)}
    return {'ok': True, 'reason': 'ready', 'profile': str(profile), 'key': str(credential)}


def decide(kind, ready):
    """Return the tier and a ledger-safe reason for one declared packet kind."""
    if kind not in KINDS:
        return {'kind': kind, 'tier': 'paid', 'reason': 'default_unknown_kind'}
    if kind in RELIABLE_KINDS:
        return {'kind': kind, 'tier': 'paid', 'reason': f'reliable_{kind}'}
    if not isinstance(ready, dict) or not ready.get('ok'):
        reason = ready.get('reason') if isinstance(ready, dict) else 'free_unprovisioned'
        return {'kind': kind, 'tier': 'paid',
                'reason': reason if reason in REASONS else 'free_unprovisioned'}
    return {'kind': kind, 'tier': 'free', 'reason': 'free_first'}


def record(entry, root=None, now=None):
    """Append one decision receipt. Ledger failures never change the launch."""
    directory = state_root() if root is None else Path(root)
    try:
        directory.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = directory / LEDGER
        line = json.dumps({**entry, 'at': time.time() if now is None else now},
                          sort_keys=True, ensure_ascii=True) + '\n'
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'a', encoding='utf-8') as ledger:
            ledger.write(line)
    except OSError:
        return None
    return path
