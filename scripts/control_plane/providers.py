"""Bounded relay probes; never log credentials, headers, or response bodies."""
from __future__ import annotations

import json
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

RELAYS = ('anyrouter', 'agentrouter')
PROBE_TIMEOUT = 5
PLUS_COOLDOWN = 5 * 3600


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_request(provider: dict) -> tuple[str, dict]:
    base = str(provider.get('base_url', '')).rstrip('/')
    parsed = urllib.parse.urlsplit(base)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('relay requires an HTTPS base_url without credentials')
    model = provider.get('probe_model')
    if not isinstance(model, str) or not model.strip() or 'astra' in model.lower():
        raise ValueError('configure a cheap non-Astra probe_model')
    wire = provider.get('wire_api')
    if wire == 'responses':
        return base + '/responses', {'model': model, 'input': 'Reply OK.',
                                     'max_output_tokens': 1, 'store': False}
    if wire == 'chat':
        return base + '/chat/completions', {'model': model, 'max_tokens': 1,
            'messages': [{'role': 'user', 'content': 'Reply OK.'}]}
    raise ValueError('unsupported wire_api')


def probe(provider: dict, modes: Path) -> dict:
    ident = provider.get('id')
    if ident not in RELAYS:
        return {'ok': False, 'configured': False, 'status': None}
    try:
        url, body = probe_request(provider)
        key_file = modes / 'keys' / ident
        if key_file.is_symlink() or key_file.stat().st_mode & 0o777 != 0o600:
            raise ValueError('private key mode required')
        key = key_file.read_text().strip()
        if not key:
            raise ValueError('empty key')
    except (OSError, ValueError):
        return {'ok': False, 'configured': False, 'status': None}
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=PROBE_TIMEOUT) as response:
            status = response.status
            payload = json.loads(response.read(64 * 1024))
            good = isinstance(payload, dict) and not payload.get('error') and (
                payload.get('object') in {'response', 'chat.completion'} or
                isinstance(payload.get('choices'), list))
            return {'ok': status == 200 and good, 'configured': True, 'status': status}
    except urllib.error.HTTPError as exc:
        exc.close()
        return {'ok': False, 'configured': True, 'status': exc.code}
    except (OSError, ValueError, urllib.error.URLError):
        return {'ok': False, 'configured': True, 'status': None}


def choose(registry: dict, previous: dict, now: float, probe_fn) -> dict:
    rows = {row.get('id'): row for row in registry.get('providers', []) if isinstance(row, dict)}
    results = {}
    for ident in RELAYS:
        result = probe_fn(rows[ident]) if ident in rows else {
            'ok': False, 'configured': False, 'status': None}
        results = {**results, ident: result}
        if result.get('ok'):
            return selected(ident, previous, now, results)
    cooldown = max(previous.get('cooldown_until', 0), previous.get('plus_cooldown_until', 0))
    configured = all(result.get('configured', True) for result in results.values())
    if cooldown > now or not configured:
        return {**previous, 'ok': False, 'checked_at': now, 'probes': results,
                'cooldown_until': cooldown, 'reason': 'cooldown' if cooldown > now else 'probe_configuration'}
    return selected('openai', previous, now, results)


def selected(ident: str, previous: dict, now: float, probes: dict) -> dict:
    return {'ok': True, 'id': ident, 'checked_at': now, 'cooldown_until': 0,
            'plus_cooldown_until': max(previous.get('plus_cooldown_until', 0), previous.get('cooldown_until', 0)),
            'changed_at': previous.get('changed_at', now) if previous.get('id') == ident else now,
            'probes': probes}
