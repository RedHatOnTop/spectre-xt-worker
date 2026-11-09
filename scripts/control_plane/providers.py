"""Bounded relay probes and top-level provider rank.

Rank (locked 2026-09-22): agentrouter → anyrouter → kimi_free → openai(Plus).
Never log credentials, headers, or response bodies.

A probe success is conclusive and a failure is not: a relay can refuse a small
synthetic request yet serve the genuine client. A failed or unprobeable relay
therefore raises an alert and the chain moves on; it never parks the chain.
"""
from __future__ import annotations

import http.client
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
# The anyrouter SSE shim listens on plain http; every other relay needs https.
LOOPBACK_HOSTS = frozenset({'127.0.0.1', '::1'})
ERROR_BODY_MAX = 4096
# The Responses API rejects max_output_tokens below 16 with a 400, so its
# smallest valid probe is 16 tokens; chat accepts one.
RESPONSES_MIN_OUTPUT = 16
# Error bodies measured on anyrouter, checked in order. A bogus model id gets
# the generic overload shape byte for byte, so that shape is classified by
# status alone and never names the configured model.
BODY_SHAPES = (
    ('Invalid URL (', 'bad_route'),
    ('不支持所选模型', 'not_offered'),
    ('1m 上下文', 'needs_beta'),
    ('已下线', 'retired'),
)
# Failures no retry can fix. They alert until the configuration changes.
CONFIG_FAILURES = frozenset({'configuration', 'bad_route', 'not_offered',
                             'needs_beta', 'retired', 'unauthorized', 'rejected',
                             'invalid_response'})


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def probe_request(provider: dict) -> tuple[str, dict]:
    """URL and smallest valid body for a relay; a ValueError carries a reason code."""
    base = str(provider.get('base_url', '')).rstrip('/')
    try:
        parsed = urllib.parse.urlsplit(base)
    except ValueError:
        raise ValueError('base_url_invalid') from None
    schemes = {'http', 'https'} if parsed.hostname in LOOPBACK_HOSTS else {'https'}
    if parsed.scheme not in schemes or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('base_url_invalid')
    model = provider.get('probe_model')
    if not isinstance(model, str) or not model.strip():
        raise ValueError('probe_model_missing')
    if 'astra' in model.lower():
        raise ValueError('probe_model_astra')
    wire = provider.get('wire_api')
    if wire == 'responses':
        return base + '/responses', {'model': model, 'input': 'Reply OK.',
                                     'max_output_tokens': RESPONSES_MIN_OUTPUT,
                                     'store': False}
    if wire == 'chat':
        return base + '/chat/completions', {'model': model, 'max_tokens': 1,
            'messages': [{'role': 'user', 'content': 'Reply OK.'}]}
    raise ValueError('wire_api_unsupported')


def probe(provider: dict, modes: Path) -> dict:
    ident = provider.get('id')
    if ident not in RELAYS:
        return unconfigured('relay_unknown')
    try:
        url, body = probe_request(provider)
    except ValueError as exc:
        return unconfigured(str(exc))
    key = relay_key(modes / 'keys' / ident)
    if not key:
        return unconfigured('key_unusable')
    request = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST',
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'})
    started = time.monotonic()
    try:
        opener = urllib.request.build_opener(NoRedirect())
        with opener.open(request, timeout=PROBE_TIMEOUT) as response:
            status = response.status
            try:
                payload = json.loads(response.read(64 * 1024))
            except ValueError:
                payload = None
            good = isinstance(payload, dict) and not payload.get('error') and (
                payload.get('object') in {'response', 'chat.completion'} or
                isinstance(payload.get('choices'), list))
            ttft_ms = int((time.monotonic() - started) * 1000)
            ok = status == 200 and good
            return {'ok': ok, 'configured': True, 'status': status, 'ttft_ms': ttft_ms,
                    'degraded': ok and ttft_ms > TTFT_DEGRADED_MS,
                    'upstream_quota': False, 'failure': None if ok else 'invalid_response'}
    except urllib.error.HTTPError as exc:
        return {'ok': False, 'configured': True, 'status': exc.code, 'ttft_ms': None,
                'degraded': False, 'upstream_quota': exc.code == 402,
                'failure': classify(exc.code, error_body(exc))}
    except (OSError, ValueError, http.client.HTTPException):
        return {'ok': False, 'configured': True, 'status': None, 'ttft_ms': None,
                'degraded': False, 'upstream_quota': False, 'failure': 'unreachable'}


def unconfigured(reason: str) -> dict:
    return {'ok': False, 'configured': False, 'status': None,
            'failure': 'configuration', 'reason': reason}


def relay_key(path: Path) -> str:
    """The relay key when it is a private regular file, else ''."""
    try:
        if path.is_symlink() or path.stat().st_mode & 0o777 != 0o600:
            return ''
        return path.read_text().strip()
    except (OSError, ValueError):
        return ''


def error_body(exc: urllib.error.HTTPError) -> bytes:
    try:
        return exc.read(ERROR_BODY_MAX) or b''
    except (OSError, ValueError, http.client.HTTPException):
        return b''
    finally:
        exc.close()


def classify(status: int, body: bytes) -> str:
    """Failure class of an HTTP error: body shape first, then status."""
    try:
        text = json.dumps(json.loads(body), ensure_ascii=False)
    except ValueError:
        text = body.decode('utf-8', 'replace')
    for marker, failure in BODY_SHAPES:
        if marker in text:
            return failure
    if status == 402:
        return 'upstream_quota'
    if status in (401, 403):
        return 'unauthorized'
    if status == 429 or status >= 500:
        return 'no_serving_channel'
    return 'rejected'


def rank(probes: dict) -> str | None:
    """Best healthy astra relay, else None. Preference order is RELAYS."""
    for ident in RELAYS:
        result = probes.get(ident) or {}
        if result.get('ok'):
            return ident
    return None


def relay_alerts(results: dict) -> list[str]:
    return sorted(f"{ident}:{result.get('reason') or result['failure']}"
                  for ident, result in results.items()
                  if isinstance(result, dict) and result.get('failure') in CONFIG_FAILURES)


def choose(registry: dict, previous: dict, now: float, probe_fn, kimi_fn=None) -> dict:
    rows = {row.get('id'): row for row in registry.get('providers', []) if isinstance(row, dict)}
    results = {}
    for ident in RELAYS:
        result = probe_fn(rows[ident]) if ident in rows else unconfigured('relay_missing')
        results = {**results, ident: result}
    alerts = relay_alerts(results)
    best = rank(results)
    if best:
        return selected(best, previous, now, results, alerts)
    kimi = (kimi_fn or _kimi_unavailable)()
    results = {**results, KIMI_FREE: kimi}
    if kimi.get('ok') and not kimi.get('exhausted'):
        return selected(KIMI_FREE, previous, now, results, alerts)
    cooldown = max(previous.get('cooldown_until', 0), previous.get('plus_cooldown_until', 0))
    if cooldown > now:
        return {**previous, 'ok': False, 'checked_at': now, 'probes': results,
                'cooldown_until': cooldown, 'reason': 'cooldown', 'alerts': alerts}
    # Probe evidence alone may spend Plus only when the operator can see it.
    return selected(LAST_RESORT, previous, now, results, [*alerts, 'plus_fallback'])


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


def selected(ident: str, previous: dict, now: float, probes: dict, alerts: list[str]) -> dict:
    return {'ok': True, 'id': ident, 'checked_at': now, 'cooldown_until': 0,
            'plus_cooldown_until': max(previous.get('plus_cooldown_until', 0),
                                      previous.get('cooldown_until', 0)),
            'changed_at': previous.get('changed_at', now) if previous.get('id') == ident else now,
            'probes': probes, 'alerts': list(alerts),
            'alerts_sent': previous.get('alerts_sent', [])}


def upstream_retry_at(result: dict, now: float) -> float:
    """402 is temporary. Do not treat it as a dead account."""
    if result.get('upstream_quota') or result.get('status') == 402:
        return now + UPSTREAM_QUOTA_BACKOFF
    return now
