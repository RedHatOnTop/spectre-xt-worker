"""Bounded relay probes and top-level provider rank.

Rank (locked 2026-09-22): agentrouter → anyrouter → kimi_free → openai(Plus).
Never log credentials, headers, or response bodies.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
import urllib.error
import urllib.parse
import urllib.request

# Preference order. agentrouter is faster and wins when alive; anyrouter is the
# slow-TTFT fallback that is often down but has generous balance.
RELAYS = ('agentrouter', 'anyrouter')
KIMI_FREE = 'kimi_free'
LAST_RESORT = 'openai'
PROBE_TIMEOUT = 5
PLUS_COOLDOWN = 5 * 3600
# Anyrouter can be alive yet unusable. Soft gate only — a degraded relay still
# beats Plus, but a healthy preferred relay preempts it on the next tick.
TTFT_DEGRADED_MS = 15_000
# 402 on agentrouter is upstream refill/crowding, not an account death.
UPSTREAM_QUOTA_BACKOFF = 10 * 60


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
    started = time.monotonic()
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=PROBE_TIMEOUT) as response:
            status = response.status
            payload = json.loads(response.read(64 * 1024))
            good = isinstance(payload, dict) and not payload.get('error') and (
                payload.get('object') in {'response', 'chat.completion'} or
                isinstance(payload.get('choices'), list))
            ttft_ms = int((time.monotonic() - started) * 1000)
            ok = status == 200 and good
            return {'ok': ok, 'configured': True, 'status': status, 'ttft_ms': ttft_ms,
                    'degraded': ok and ttft_ms > TTFT_DEGRADED_MS,
                    'upstream_quota': False}
    except urllib.error.HTTPError as exc:
        exc.close()
        return {'ok': False, 'configured': True, 'status': exc.code, 'ttft_ms': None,
                'degraded': False, 'upstream_quota': exc.code == 402}
    except (OSError, ValueError, urllib.error.URLError):
        return {'ok': False, 'configured': True, 'status': None, 'ttft_ms': None,
                'degraded': False, 'upstream_quota': False}


def rank(probes: dict) -> str | None:
    """Best healthy astra relay, else None. Preference order is RELAYS."""
    for ident in RELAYS:
        result = probes.get(ident) or {}
        if result.get('ok'):
            return ident
    return None


def choose(registry: dict, previous: dict, now: float, probe_fn, kimi_fn=None) -> dict:
    rows = {row.get('id'): row for row in registry.get('providers', []) if isinstance(row, dict)}
    results = {}
    for ident in RELAYS:
        result = probe_fn(rows[ident]) if ident in rows else {
            'ok': False, 'configured': False, 'status': None}
        results = {**results, ident: result}
    best = rank(results)
    if best:
        return selected(best, previous, now, results)
    kimi = (kimi_fn or _kimi_unavailable)()
    results = {**results, KIMI_FREE: kimi}
    if kimi.get('ok') and not kimi.get('exhausted'):
        return selected(KIMI_FREE, previous, now, results)
    cooldown = max(previous.get('cooldown_until', 0), previous.get('plus_cooldown_until', 0))
    configured = all(result.get('configured', True) for result in results.values()
                     if isinstance(result, dict) and 'configured' in result)
    if cooldown > now or not configured:
        return {**previous, 'ok': False, 'checked_at': now, 'probes': results,
                'cooldown_until': cooldown,
                'reason': 'cooldown' if cooldown > now else 'probe_configuration'}
    return selected(LAST_RESORT, previous, now, results)


def _kimi_unavailable() -> dict:
    return {'ok': False, 'configured': False, 'exhausted': True, 'status': None}


def probe_kimi(env: dict) -> dict:
    from . import cline_free, kimi, omni_setup
    endpoint = str(env.get('SPECTRE_OMNI_ENDPOINT', omni_setup.ENDPOINT))
    key_path = Path(env.get('SPECTRE_OMNI_CLIENT_KEY',
                        Path.home() / '.config/omni-proxy/client_key'))
    try:
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1'
                or not parsed.port or parsed.path != '/v1' or parsed.query or parsed.fragment
                or parsed.username or parsed.password):
            raise ValueError('invalid proxy endpoint')
        omni_setup.private_file(key_path)
        key = key_path.read_text(encoding='utf-8').strip()
    except (OSError, ValueError):
        return {'ok': False, 'configured': True, 'exhausted': False,
                'status': None, 'reason': 'proxy_config_missing'}
    result = kimi.probe(endpoint, key, timeout=5)
    if result.get('ok') or result.get('status') == 429:
        cline_free.record(None, time.time(), 200 if result.get('ok') else 429)
    return {'ok': bool(result.get('ok')), 'configured': True,
            'exhausted': result.get('status') == 429,
            'status': 200 if result.get('ok') else result.get('status'),
            'reason': None if result.get('ok') else result.get('error')}


def kimi_candidate(env: dict, usage_fn, probe_fn=None) -> dict:
    enabled = str(env.get('SPECTRE_KIMI_ENABLED', '')).lower().strip() in {
        '1', 'true', 'yes', 'on'}
    if not enabled:
        return {'ok': False, 'configured': True, 'exhausted': True,
                'status': None, 'reason': 'disabled'}
    usage = usage_fn()
    if not usage.get('ok') or usage.get('exhausted'):
        return usage
    return (probe_fn or probe_kimi)(env)


def selected(ident: str, previous: dict, now: float, probes: dict) -> dict:
    return {'ok': True, 'id': ident, 'checked_at': now, 'cooldown_until': 0,
            'plus_cooldown_until': max(previous.get('plus_cooldown_until', 0),
                                      previous.get('cooldown_until', 0)),
            'changed_at': previous.get('changed_at', now) if previous.get('id') == ident else now,
            'probes': probes}


def upstream_retry_at(result: dict, now: float) -> float:
    """402 is temporary. Do not treat it as a dead account."""
    if result.get('upstream_quota') or result.get('status') == 402:
        return now + UPSTREAM_QUOTA_BACKOFF
    return now
