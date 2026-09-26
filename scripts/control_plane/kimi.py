"""Operator-gated Kimi handoff through a visible Orca terminal."""
from __future__ import annotations

import http.client
import json
import os
from pathlib import Path
import re
import shlex
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request

from . import cline_free, inventory, orca, pins, planning, providers, runtime, seat
from .io import locked, read_json, run as run_command, write_json

MODEL_ALIAS = 'cline/kimi-k3'
UPSTREAM_MODEL = 'cline-free/kimi-k3'
HANDLE_PATTERN = re.compile(r'^term_[a-zA-Z0-9-]+$')
PLANNER_MODELS = frozenset(seat.PLANNER_ROLES.values())


def config_endpoint(path: Path, endpoint: str) -> str:
    parsed = urllib.parse.urlsplit(endpoint)
    if (parsed.scheme != 'http' or parsed.hostname != '127.0.0.1' or not parsed.port
            or parsed.path != '/v1' or parsed.query or parsed.fragment
            or parsed.username is not None or parsed.password is not None):
        raise ValueError('Kimi seat endpoint must be a loopback HTTP /v1 URL')
    if path.is_symlink() or path.stat().st_mode & 0o077:
        raise ValueError('Kimi config must be private and not a symlink')
    config = tomllib.loads(path.read_text(encoding='utf-8'))
    providers_config = config.get('providers')
    models_config = config.get('models')
    if not isinstance(providers_config, dict) or not isinstance(models_config, dict):
        raise ValueError('Kimi config provider or model map is invalid')
    provider = providers_config.get('cline')
    model = models_config.get(MODEL_ALIAS)
    if not isinstance(provider, dict) or not isinstance(model, dict):
        raise ValueError('Kimi config provider or model entry is invalid')
    base_url = provider.get('base_url')
    key = provider.get('api_key', '')
    if (provider.get('type') != 'openai' or not isinstance(base_url, str)
            or base_url.rstrip('/') != endpoint or not isinstance(key, str)
            or model.get('provider') != 'cline' or model.get('model') != UPSTREAM_MODEL):
        raise ValueError('Kimi config does not target the selected omni-proxy model')
    return key


def probe(endpoint: str, key: str, timeout: float = 15) -> dict:
    body = json.dumps({'model': UPSTREAM_MODEL, 'max_tokens': 1,
                       'messages': [{'role': 'user', 'content': 'Reply OK.'}]}).encode()
    headers = {'Content-Type': 'application/json'}
    if key:
        headers = {**headers, 'Authorization': f'Bearer {key}'}
    request = urllib.request.Request(endpoint + '/chat/completions', data=body,
                                     headers=headers, method='POST')
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                          providers.NoRedirect()).open(
                request, timeout=timeout) as response:
            payload = json.loads(response.read(64 * 1024))
            correct = (response.headers.get('X-Omni-Provider') == 'cline-free'
                       and response.headers.get('X-Omni-Attempts') == '1'
                       and response.headers.get('X-Omni-Fallback', 'false').lower() != 'true')
            choices = payload.get('choices') if isinstance(payload, dict) else None
            if response.status != 200 or not correct or not isinstance(choices, list) or not choices:
                return {'ok': False, 'error': 'kimi_probe_invalid_response'}
            return {'ok': True}
    except urllib.error.HTTPError as exc:
        exc.close()
        return {'ok': False, 'error': 'kimi_probe_http', 'status': exc.code}
    except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException):
        return {'ok': False, 'error': 'kimi_probe_unavailable'}


def context(workers_file: Path, provider_state: Path, loop_state: Path,
            seat_root: Path, client, now: float, env: dict, resume: str | None = None) -> dict:
    if not seat_root.is_absolute():
        raise ValueError('SPECTRE_SEAT_ROOT must be absolute')
    if not runtime.enabled(env, 'SPECTRE_KIMI_ENABLED'):
        raise ValueError('SPECTRE_KIMI_ENABLED is off')
    workers = read_json(workers_file).get('workers')
    if not isinstance(workers, dict):
        raise ValueError('worker registry is invalid')
    runtime.validate_workers(workers)
    entry = workers.get('minecraft')
    if not entry or not entry.get('planner'):
        raise ValueError('minecraft planner worker missing')
    current = seat.load(seat.seat_path(seat_root))
    # A confirm that committed the seat and then failed to write the pin or the loop
    # state is finished by confirming the same terminal again, whatever the provider
    # selection and the goal did in the meantime.
    resumed = (resume is not None and current['owner'] == seat.OWNER_KIMI
               and current.get('terminal') == resume)
    if not resumed:
        health = read_json(provider_state)
        checked_at = health.get('checked_at')
        if (not health.get('ok') or health.get('id') != 'kimi_free'
                or type(checked_at) not in {int, float} or now - checked_at > 600
                or checked_at > now + 30):
            raise ValueError('fresh kimi_free provider selection required')
    state = read_json(loop_state)
    loop_workers = state.get('workers')
    if not isinstance(loop_workers, dict) or not isinstance(loop_workers.get('minecraft'), dict):
        raise ValueError('loop worker state is invalid')
    pending = loop_workers['minecraft'].get('handoff')
    if (not isinstance(pending, dict) or pending.get('to') != seat.OWNER_KIMI
            or pending.get('brief') != str(seat.brief_path(seat_root))):
        raise ValueError('current authoritative Kimi handoff required')
    if not resumed:
        snapshot = client.snapshot('minecraft')
        if (snapshot.get('goal', {}).get('state') not in {'COMPLETED', 'FAILED'}
                or snapshot.get('policy', {}).get('continuity_recovery_allowed')
                or pending.get('origin') != planning.identity(snapshot)):
            raise ValueError('current authoritative Kimi handoff required')
        if current['owner'] != seat.OWNER_ASTRA:
            raise ValueError(f"seat_owned_by_{current['owner']}")
        if not seat.can_handoff(current, now)[0]:
            raise ValueError('handoff_day_cap')
    brief = seat.brief_path(seat_root)
    if brief.is_symlink() or not brief.is_file() or not brief.read_text().strip():
        raise ValueError('nonempty handoff brief required')
    return {'entry': entry, 'state': state, 'pending': pending, 'seat': current,
            'brief': brief, 'resumed': resumed}


def set_planner(workers_file: Path, planner: dict) -> None:
    with locked(workers_file.with_suffix('.lock')):
        registry = read_json(workers_file)
        current = registry['workers']['minecraft']
        write_json(workers_file, {**registry, 'workers': {
            **registry['workers'], 'minecraft': {**current, 'planner': planner}}})


def orca_terminals(run) -> list[dict]:
    result = run(['orca-ide', 'terminal', 'list', '--json'], timeout=15)
    if not result.get('ok'):
        raise RuntimeError('orca_list_failed')
    terminals = result.get('parsed', {}).get('result', {}).get('terminals')
    if not isinstance(terminals, list):
        raise RuntimeError('orca_inventory_invalid')
    return terminals


def live_terminal(terminals: list[dict], processes: list[dict], cwd: str,
                  handle: str, role: str) -> bool:
    return (any(row.get('handle') == handle and row.get('worktreePath') == cwd
                and row.get('connected') and row.get('writable') for row in terminals)
            and len([row for row in processes if row.get('handle') == handle
                     and row.get('cwd') == cwd and row.get('model') == role]) == 1)


def launch(workers_file: Path, provider_state: Path, loop_state: Path, seat_root: Path,
           kimi_config: Path, client, *, now=None, env=None, run=run_command,
           scan=inventory.scan, probe_fn=probe, dry=False) -> dict:
    now = time.time() if now is None else now
    env = dict(os.environ) if env is None else env
    with locked(loop_state.with_suffix('.lock')):
        info = context(workers_file, provider_state, loop_state, seat_root, client, now, env)
        if info['pending'].get('terminal'):
            return {'ok': False, 'error': 'kimi_terminal_already_created',
                    'terminal': info['pending']['terminal']}
        processes = scan()
        if any(row.get('model') in PLANNER_MODELS and row.get('cwd') == info['entry']['cwd']
               for row in processes):
            return {'ok': False, 'error': 'top_level_agent_already_running'}
        terminals = orca_terminals(run)
        efficient = pins.pick(terminals, processes, info['entry']['cwd'], 'efficient')
        if not efficient['ok']:
            return {'ok': False, 'error': 'efficient_pin_required_before_second_terminal'}
        standby = info['seat'].get('standby')
        endpoint = standby.get('endpoint') if isinstance(standby, dict) else None
        if not isinstance(endpoint, str):
            raise ValueError('Kimi seat endpoint is missing')
        key = config_endpoint(kimi_config, endpoint)
        kimi_bin = kimi_config.parent / 'bin' / 'kimi'
        if kimi_bin.is_symlink() or not kimi_bin.is_file() or not os.access(kimi_bin, os.X_OK):
            raise ValueError('Kimi Code executable is missing or not executable')
        if dry:
            return {'ok': True, 'dry_run': True, 'worktree': info['entry']['cwd'],
                    'probe': 'skipped'}
        checked = probe_fn(endpoint, key)
        if not checked.get('ok'):
            if checked.get('status') == 429:
                cline_free.record(None, now, 429)
                write_json(provider_state, {**read_json(provider_state), 'ok': False,
                                            'reason': 'kimi_free_exhausted'})
            return checked
        created = orca.create(info['entry']['cwd'], 'kimi-standby',
                              f'exec {shlex.quote(str(kimi_bin))} -m {MODEL_ALIAS}', run)
        if not created['ok']:
            return created
        handle = created['handle']
        if not HANDLE_PATTERN.fullmatch(handle):
            return {'ok': False, 'error': 'orca_handle_missing; inspect terminal list before retry'}
        ws = info['state']['workers']['minecraft']
        pending = {**info['pending'], 'terminal': handle, 'launched_at': now}
        write_json(loop_state, runtime.worker_state(info['state'], 'minecraft',
                                                   {**ws, 'handoff': pending}))
        return {'ok': True, 'terminal': handle, 'worktree': info['entry']['cwd'],
                'next': 'send_prompt'}


def send_prompt(workers_file: Path, provider_state: Path, loop_state: Path,
                seat_root: Path, client, handle: str, *, now=None, env=None,
                run=run_command, scan=inventory.scan) -> dict:
    now = time.time() if now is None else now
    env = dict(os.environ) if env is None else env
    if not HANDLE_PATTERN.fullmatch(handle):
        raise ValueError('invalid terminal handle')
    with locked(loop_state.with_suffix('.lock')):
        info = context(workers_file, provider_state, loop_state, seat_root, client, now, env)
        if info['pending'].get('terminal') != handle:
            raise ValueError('terminal does not match pending handoff')
        if info['pending'].get('prompt_sent_at'):
            return {'ok': False, 'error': 'kimi_prompt_already_sent'}
        if info['pending'].get('prompt_uncertain_at'):
            return {'ok': False, 'error': 'kimi_prompt_uncertain; inspect terminal before retry'}
        if not live_terminal(orca_terminals(run), scan(), info['entry']['cwd'], handle, 'kimi'):
            return {'ok': False, 'error': 'kimi_terminal_unconfirmed'}
        prompt = seat.cold_start_prompt(info['seat'], seat_root)
        sent = run(['orca-ide', 'terminal', 'send', '--terminal', handle,
                    '--text', prompt, '--enter', '--json'], timeout=15)
        if not sent.get('ok'):
            if sent.get('uncertain'):
                ws = info['state']['workers']['minecraft']
                pending = {**info['pending'], 'prompt_uncertain_at': now}
                write_json(loop_state, runtime.worker_state(info['state'], 'minecraft',
                                                           {**ws, 'handoff': pending}))
            return {'ok': False, 'error': 'kimi_prompt_send_unconfirmed'}
        ws = info['state']['workers']['minecraft']
        pending = {**info['pending'], 'prompt_sent_at': now}
        write_json(loop_state, runtime.worker_state(info['state'], 'minecraft',
                                                   {**ws, 'handoff': pending}))
        return {'ok': True, 'terminal': handle, 'next': 'confirm_after_observing_reply'}


def confirm(workers_file: Path, provider_state: Path, loop_state: Path,
            seat_root: Path, client, handle: str, *, observed_ready=False, now=None,
            env=None, run=run_command, scan=inventory.scan) -> dict:
    if not observed_ready:
        raise ValueError('explicit observed-ready confirmation required')
    now = time.time() if now is None else now
    env = dict(os.environ) if env is None else env
    if not HANDLE_PATTERN.fullmatch(handle):
        raise ValueError('invalid terminal handle')
    with locked(loop_state.with_suffix('.lock')):
        info = context(workers_file, provider_state, loop_state, seat_root, client, now, env,
                       resume=handle)
        if (info['pending'].get('terminal') != handle
                or not info['pending'].get('prompt_sent_at')):
            raise ValueError('prompted Kimi terminal required')
        if not live_terminal(orca_terminals(run), scan(), info['entry']['cwd'], handle, 'kimi'):
            return {'ok': False, 'error': 'kimi_terminal_unconfirmed'}
        if not info['resumed']:
            updated = seat.transition(info['seat'], now, to=seat.OWNER_KIMI,
                                      brief=str(info['brief']))
            if not updated['ok']:
                return updated
            committed = seat.commit_if_current(seat.seat_path(seat_root), info['seat'],
                                               {**updated['seat'], 'terminal': handle})
            if not committed['ok']:
                return committed
        try:
            set_planner(workers_file, {'terminal': handle, 'harness': seat.OWNER_KIMI,
                                       'model': UPSTREAM_MODEL, 'provider': providers.KIMI_FREE})
        except (OSError, ValueError, KeyError):
            return {'ok': False, 'error': 'registry_update_failed',
                    'seat_committed': True, 'terminal': handle}
        ws = info['state']['workers']['minecraft']
        try:
            write_json(loop_state, runtime.worker_state(info['state'], 'minecraft',
                                                       {**ws, 'handoff': None}))
        except OSError:
            return {'ok': False, 'error': 'loop_state_update_failed',
                    'seat_committed': True, 'terminal': handle}
        return {'ok': True, 'owner': seat.OWNER_KIMI, 'terminal': handle,
                **({'resumed': True} if info['resumed'] else {})}


def release(workers_file: Path, provider_state: Path, loop_state: Path,
            seat_root: Path, *, observed_stopped=False, now=None,
            scan=inventory.scan, owner=seat.OWNER_KIMI) -> dict:
    if not observed_stopped:
        raise ValueError('explicit observed-stopped confirmation required')
    if not seat_root.is_absolute():
        raise ValueError('SPECTRE_SEAT_ROOT must be absolute')
    now = time.time() if now is None else now
    with locked(loop_state.with_suffix('.lock')):
        workers = read_json(workers_file).get('workers')
        if not isinstance(workers, dict):
            raise ValueError('worker registry is invalid')
        runtime.validate_workers(workers)
        entry = workers.get('minecraft')
        if not entry or not entry.get('planner'):
            raise ValueError('minecraft planner worker missing')
        health = read_json(provider_state)
        checked_at = health.get('checked_at')
        if (not health.get('ok') or health.get('id') not in {*providers.RELAYS,
                                                             providers.LAST_RESORT}
                or type(checked_at) not in {int, float} or now - checked_at > 600
                or checked_at > now + 30):
            raise ValueError('fresh Astra provider selection required')
        current = seat.load(seat.seat_path(seat_root))
        if current['owner'] != owner:
            raise ValueError(f"seat_owned_by_{current['owner']}")
        if any(row.get('model') in PLANNER_MODELS and row.get('cwd') == entry['cwd']
               for row in scan()):
            return {'ok': False, 'error': 'top_level_agent_still_running'}
        brief = seat.brief_path(seat_root)
        if brief.is_symlink() or not brief.is_file() or not brief.read_text().strip():
            raise ValueError('nonempty handoff brief required')
        updated = seat.transition(current, now, to=seat.OWNER_ASTRA, brief=str(brief))
        if not updated['ok']:
            return updated
        committed = seat.commit_if_current(seat.seat_path(seat_root), current,
                                           updated['seat'])
        if not committed['ok']:
            return committed
        try:
            set_planner(workers_file, {'terminal': None, 'harness': seat.OWNER_ASTRA,
                                       'model': 'gpt-6-astra'})
        except (OSError, ValueError, KeyError):
            return {'ok': False, 'error': 'registry_update_failed', 'seat_committed': True}
        return {'ok': True, 'owner': seat.OWNER_ASTRA, 'next': 'launch_astra'}
