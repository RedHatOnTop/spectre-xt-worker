"""Persist bounded incident receipts, not worker lifecycle authority."""
import hashlib
import json
from pathlib import Path
import subprocess
import time

ALERT_INTERVAL = 900
NOTIFY = '/usr/local/bin/spectre-slack-notify'


def config(path):
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict) or data.get('schema_version') != 1:
        raise ValueError('runtime config requires schema_version 1')
    paths = data.get('required_workspaces')
    if not isinstance(paths, list) or any(not isinstance(p, str) or not p.startswith('/') for p in paths):
        raise ValueError('required_workspaces must contain absolute paths')
    return data


def read_receipt(path):
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError('runtime receipt is not an object')
    return data


def write_receipt(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(data, indent=2) + '\n')
    temporary.chmod(0o600)
    temporary.replace(path)


def incident_identity(issue):
    fields = {key: issue.get(key) for key in ('code', 'dispatch_id', 'run_id', 'path', 'error')}
    projection = issue.get('projection') or {}
    liveness = issue.get('liveness') or projection.get('liveness') or {}
    return {**fields, 'verdict': liveness.get('verdict'), 'reason': liveness.get('reason'),
            'categories': (projection.get('attention') or {}).get('categories', [])}


def notify(report, path, now=None):
    stamp = time.time() if now is None else now
    before = read_receipt(path)
    identities = [incident_identity(issue) for issue in report['issues']]
    fingerprint = hashlib.sha256(json.dumps(identities, sort_keys=True).encode()).hexdigest()
    same = before.get('fingerprint') == fingerprint
    if not report['issues']:
        write_receipt(path, {'fingerprint': fingerprint, 'last_alert': 0, 'status': report['status']})
        return 'clear'
    if same and stamp - before.get('last_alert', 0) < ALERT_INTERVAL:
        return 'throttled'
    text = 'Spectre native runtime needs attention: ' + json.dumps(identities, ensure_ascii=False)
    result = subprocess.run([NOTIFY, '--agent', 'bridge', '--channel', 'fleet', '--text', text],
                            capture_output=True, text=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError(f'Runtime alert exited {result.returncode}\n{result.stdout}{result.stderr}')
    write_receipt(path, {'fingerprint': fingerprint, 'last_alert': stamp, 'status': report['status']})
    return 'sent'
