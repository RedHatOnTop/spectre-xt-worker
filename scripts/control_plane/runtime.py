"""One bounded tick: claim, persist intent, then invoke the sole terminal writer."""
from __future__ import annotations

from pathlib import Path
import time

from . import budget, cline_free, packets, planning, quota, seat
from .io import locked, read_json, write_json, run as run_command

TICK_SECONDS = 45


def enabled(env: dict, key: str) -> bool:
    return str(env.get(key, '')).lower().strip() in {'1', 'true', 'yes', 'on'}


def worker_state(state: dict, worker: str, value: dict) -> dict:
    return {**state, 'workers': {**state.get('workers', {}), worker: value}}


def validate_workers(workers: dict) -> None:
    allowed_class = {'toplevel', 'side'}
    for name, entry in workers.items():
        if (not isinstance(entry, dict) or not isinstance(entry.get('cwd'), str)
                or not Path(entry['cwd']).is_absolute()):
            raise ValueError(f'{name}: absolute cwd required')
        worker_class = entry.get('class')
        if worker_class is None:
            worker_class = 'toplevel' if (entry.get('planner') or entry.get('seat')) else 'side'
        if worker_class not in allowed_class:
            raise ValueError(f'{name}: class must be toplevel or side')
        if entry.get('planner') and name != 'minecraft':
            raise ValueError('planner is legal only on minecraft')
        if entry.get('planner') and worker_class != 'toplevel':
            raise ValueError('planner requires class=toplevel')
        if entry.get('seat') and worker_class != 'toplevel':
            raise ValueError('seat is legal only on toplevel workers')


def notify(run, env: dict, worker: str, reason: str, *, channel='lobby') -> dict:
    return run([env.get('SPECTRE_NOTIFY_BIN', 'spectre-slack-notify'), '--agent', 'loop',
        '--channel', channel, '--text', f'{worker}: {reason}'], environ=env, timeout=5)


def dispatch(run, env: dict, argv: list[str]) -> dict:
    return run([env.get('SPECTRE_DISPATCH_BIN', 'spectre-slack-bridge'), '--dispatch', *argv],
               environ=env, timeout=8)


def escalation(worker, ws, reason, run, env):
    if ws.get('notified') == reason:
        return ws, {'worker': worker, 'action': 'sit', 'reason': reason}
    channel = 'fleet' if reason.startswith('idle_slo:') else 'lobby'
    out = notify(run, env, worker, reason, channel=channel)
    updated = {**ws, 'notified': reason} if out.get('ok') else ws
    return updated, {'worker': worker, 'action': 'escalate', 'reason': reason, 'io': out}


def dry_action(worker, entry, snapshot, env, path, now):
    policy = snapshot.get('policy', {})
    planner = entry.get('planner')
    if planner and not enabled(env, 'ASTRA_ENABLED'):
        return {'worker': worker, 'action': 'skip', 'reason': 'planner_pin'}
    if not policy.get('grokbot_may_advance'):
        return {'worker': worker, 'action': 'skip', 'reason': snapshot.get('goal', {}).get('state')}
    if not planner:
        return {'worker': worker, 'action': 'goal'}
    if read_json(path).get('workers', {}).get(worker, {}).get('claude_handoff'):
        return {'worker': worker, 'action': 'skip', 'reason': 'claude_handoff_pending'}
    owner = seat.load(seat.seat_path(seat_root(path, env), worker))['owner']
    reason = pin_refusal(planner, owner)
    if not reason and owner == seat.OWNER_ASTRA:
        reason = codex_refusal(planner, read_json(provider_path(env)), read_json(path), snapshot, now)
    elif not reason and owner == seat.OWNER_KIMI:
        reason = kimi_refusal(now)
    reason = reason or (None if planner.get('terminal') else 'planner_terminal_missing')
    if reason:
        return {'worker': worker, 'action': 'escalate', 'reason': reason}
    return {'worker': worker, 'action': 'plan'}


def codex_refusal(planner, provider, state, snapshot, now):
    # codex_gate() also writes the Kimi handoff brief; a dry run only names the verdict.
    reason = budget.planner_refusal(state, provider, now, snapshot['goal']['state'] == 'FAILED')
    if reason:
        return reason
    if provider.get('id') == 'kimi_free':
        return 'kimi_handoff_required'
    if planner.get('provider') and planner['provider'] != provider.get('id'):
        return 'provider_restart_required'
    return None


def kimi_refusal(now):
    # While the relays are down the provider tick probes Kimi and records the free quota's
    # first 429; a plan typed after it would only time out, and each timeout alerts anew.
    return 'kimi_free_exhausted' if cline_free.status(cline_free.load(), now)['exhausted'] else None


def provider_path(env):
    return Path(env.get('SPECTRE_PROVIDER_STATE', Path.home() / '.local/state/remote-agent/codex-provider.json'))


def pin_refusal(planner: dict, owner: str) -> str | None:
    if (planner.get('harness') or seat.OWNER_ASTRA) == owner:
        return None
    return 'planner_harness_mismatch' if owner == seat.OWNER_ASTRA else f'seat_owned_by_{owner}'


def seat_root(path, env):
    root = Path(env.get('SPECTRE_SEAT_ROOT', path.parent))
    if not root.is_absolute():
        raise ValueError('SPECTRE_SEAT_ROOT must be absolute')
    return root


def planner_seat(worker, snapshot, state, path, env, provider, now, run):
    ws = state.get('workers', {}).get(worker, {})
    root = seat_root(path, env)
    current = seat.load(seat.seat_path(root, worker))
    if current['owner'] != seat.OWNER_ASTRA:
        updated, action = escalation(worker, ws, f"seat_owned_by_{current['owner']}", run, env)
        return worker_state(state, worker, updated), action
    if provider.get('id') == 'kimi_free':
        pending = ws.get('handoff')
        if not pending or pending.get('origin') != planning.identity(snapshot):
            prepared = seat.prepare_handoff(current, root, now, goal=(
                f"Goal ID: {snapshot['goal'].get('goal_id') or 'unknown'}"),
                decisions=['Operator must confirm the visible Kimi seat before ownership changes.'],
                outcomes=[f"Authoritative state: {snapshot['goal']['state']}"])
            if not prepared['ok']:
                updated, action = escalation(worker, ws, prepared['error'], run, env)
                return worker_state(state, worker, updated), action
            ws = {**ws, 'handoff': {'to': seat.OWNER_KIMI,
                                   'origin': planning.identity(snapshot),
                                   'brief': prepared['brief']['path'], 'requested_at': now}}
            state = worker_state(state, worker, ws)
            write_json(path, state)
        updated, action = escalation(worker, ws, 'kimi_handoff_required', run, env)
        return worker_state(state, worker, updated), action
    if ws.get('handoff'):
        state = worker_state(state, worker, {**ws, 'handoff': None, 'notified': None})
    return state, None


def codex_gate(worker, entry, snapshot, state, path, env, now, run):
    ws = state.get('workers', {}).get(worker, {})
    planner = entry.get('planner', {})
    provider = read_json(provider_path(env))
    reason = budget.planner_refusal(state, provider, now, snapshot['goal']['state'] == 'FAILED')
    if reason:
        new_ws, action = escalation(worker, ws, reason, run, env)
        return worker_state(state, worker, new_ws), action, None
    state, seat_action = planner_seat(worker, snapshot, state, path, env, provider, now, run)
    if seat_action:
        return state, seat_action, None
    ws = state.get('workers', {}).get(worker, {})
    if planner.get('provider') and planner['provider'] != provider.get('id'):
        new_ws, action = escalation(worker, ws, 'provider_restart_required', run, env)
        return worker_state(state, worker, new_ws), action, None
    return state, None, provider.get('id')


def start_plan(worker, entry, snapshot, state, path, client, env, now, run):
    # spectre-claude stops the old planner before it launches Claude; a plan requested
    # meanwhile would sit unanswered and block the confirm.
    if state.get('workers', {}).get(worker, {}).get('claude_handoff'):
        return state, {'worker': worker, 'action': 'skip', 'reason': 'claude_handoff_pending'}
    planner = entry.get('planner', {})
    owner = seat.load(seat.seat_path(seat_root(path, env), worker))['owner']
    refusal = pin_refusal(planner, owner)
    if refusal:
        ws = state.get('workers', {}).get(worker, {})
        new_ws, action = escalation(worker, ws, refusal, run, env)
        return worker_state(state, worker, new_ws), action
    if owner == seat.OWNER_ASTRA:
        state, action, ledger = codex_gate(worker, entry, snapshot, state, path, env, now, run)
        if action:
            return state, action
    else:
        # Codex budgets and provider health do not describe another harness.
        refusal = kimi_refusal(now) if owner == seat.OWNER_KIMI else None
        if refusal:
            ws = state.get('workers', {}).get(worker, {})
            new_ws, action = escalation(worker, ws, refusal, run, env)
            return worker_state(state, worker, new_ws), action
        ledger = seat.seats_for_owner(owner)[0]['provider']
    ws = state.get('workers', {}).get(worker, {})
    if not planner.get('terminal'):
        new_ws, action = escalation(worker, ws, 'planner_terminal_missing', run, env)
        return worker_state(state, worker, new_ws), action
    current = planning.request(snapshot, now, seat.PLAN_TIMEOUTS.get(owner, planning.ASSIGNMENT_TIMEOUT))
    state = worker_state(state, worker, {**ws, 'planning': current})
    write_json(path, state)
    claim = planning.advance(client, worker, snapshot, current['request_id'])
    current = {**current, 'action_id': claim['action_id'], 'status': 'sending'}
    state = worker_state(state, worker, {**ws, 'planning': current})
    write_json(path, state)
    planning.event(client, worker, 'assignment.started', current, now)
    plans = [row for row in state.get('plans', []) if row['at'] > now - budget.PLUS_WINDOW]
    state = {**state, 'plans': [*plans, {'at': now, 'provider': ledger,
                                       'critical': snapshot['goal']['state'] == 'FAILED'}]}
    write_json(path, state)
    out = dispatch(run, env, ['plan', worker, '--request-id', current['request_id']])
    current = {**current, 'status': 'waiting', 'delivery': out}
    return worker_state(state, worker, {**ws, 'planning': current}), {
        'worker': worker, 'action': 'plan', 'request_id': current['request_id'], 'io': out}


def finish_plan(worker, ws, state, client, env, now, run):
    current = ws['planning']
    snapshot = client.snapshot(worker)
    if planning.identity(snapshot) != current['origin']:
        updated = {**ws, 'planning': None, 'queue': []}
        return worker_state(state, worker, updated), {'worker': worker, 'action': 'skip', 'reason': 'superseded_assignment'}
    if 'action_id' not in current:
        snapshot = client.snapshot(worker)
        claim = planning.advance(client, worker, snapshot, current['request_id'])
        current = {**current, 'action_id': claim['action_id']}
    planning.event(client, worker, 'assignment.started', current, now)
    directory = Path(env.get('SPECTRE_PACKET_DIR', Path.home() / '.local/state/remote-agent/packets'))
    result = planning.result(directory, current, now)
    if result['action'] == 'ready':
        planning.event(client, worker, 'assignment.finished', current, now)
        updated = {**ws, 'planning': None, 'queue': result['packets'],
                   'request_id': current['request_id'], 'notified': None}
        return worker_state(state, worker, updated), None
    if result['action'] == 'timeout':
        planning.event(client, worker, 'assignment.failed', current, now)
        updated = {**ws, 'planning': None, 'retry_after': now + ASSIGNMENT_RETRY,
                   'issue': f"assignment_timeout:{current['request_id']}"}
        updated, action = escalation(worker, updated, updated['issue'], run, env)
        return worker_state(state, worker, updated), action
    updated = {**ws, 'planning': {**current, **{key: result[key] for key in ('file_stamp',) if key in result}}}
    return worker_state(state, worker, updated), {'worker': worker, 'action': 'waiting'}


ASSIGNMENT_RETRY = 300


def next_packet(worker, entry, snapshot, ws):
    queue = ws.get('queue') or []
    if queue:
        return queue[0]
    if entry.get('planner'):
        return None
    goal = packets.next_goal(entry['cwd'])
    return {'goal': goal, 'assignee': 'efficient', 'kind': 'mechanical'} if goal else None


def execute_packet(worker, item, snapshot, state, path, client, env, now, run):
    ws = state.get('workers', {}).get(worker, {})
    reason = budget.refusal(state, worker, item['goal'], now)
    if reason:
        if reason == 'dispatch_spacing':
            return state, {'worker': worker, 'action': 'skip', 'reason': reason}
        updated, action = escalation(worker, ws, reason, run, env)
        return worker_state(state, worker, updated), action
    if worker == 'minecraft' and enabled(env, 'SPECTRE_FREE_PACKETS_ENABLED'):
        item = quota.route_packet(item, quota.free_status_now(now, env=env))
    origin = planning.identity(snapshot)
    state = budget.reserve(state, worker, item['goal'], now)
    ws = state['workers'][worker]
    active = {'origin': origin, 'packet': item, 'status': 'prepared',
              'request_id': f"goal-{snapshot['snapshot_version']}",
              'advance': snapshot['policy'].get('grokbot_may_advance', False)}
    state = worker_state(state, worker, {**ws, 'active': active})
    write_json(path, state)
    return send_prepared(worker, snapshot, state, path, client, env, run)


def send_prepared(worker, snapshot, state, path, client, env, run):
    ws = state['workers'][worker]
    active = ws['active']
    item = active['packet']
    if active.get('advance'):
        planning.advance(client, worker, snapshot, active['request_id'])
    state = worker_state(state, worker, {**ws, 'active': {**active, 'status': 'sending'}})
    write_json(path, state)
    args = ['goal', worker, packet_text(item), '--target', item['assignee']]
    if item.get('tier') == 'free':
        args = [*args, '--tier', 'free']
    out = dispatch(run, env, args)
    return record_dispatch(worker, item, state, out, env, run)


def packet_text(item):
    acceptance = item.get('acceptance')
    return item['goal'] if not acceptance else item['goal'] + '\nAcceptance:\n' + '\n'.join(acceptance)


def record_dispatch(worker, item, state, out, env, run):
    ws = state['workers'][worker]
    verdict = out.get('parsed') or {}
    ambiguous = out.get('uncertain') or verdict.get('evt') == 'dispatch_send_failed'
    queue = ws.get('queue') or []
    rest = [row for row in queue if row.get('id') != item.get('id')]
    if not out.get('ok') and not ambiguous:
        if verdict.get('evt') in {'dispatch_flash_busy', 'dispatch_mimo_busy'}:
            history = state.get('dispatches') or []
            released = {**state, 'dispatches': history[:-1]} if history and history[-1]['worker'] == worker else state
            updated = {**ws, 'active': None, 'last_fingerprint': None,
                       'queue': [item, *rest]}
            return worker_state(released, worker, updated), {
                'worker': worker, 'action': 'refused', 'io': out}
        if verdict.get('evt') == 'dispatch_flash_free_unavailable' and item.get('tier') == 'free':
            paid = {**item, 'assignee': item.get('paid_assignee', item['assignee']),
                    'tier': 'paid', 'reason': 'prior_free_refusal'}
            updated = {**ws, 'active': None, 'last_fingerprint': None,
                       'queue': [paid, *rest]}
            history = state.get('dispatches') or []
            released = {**state, 'dispatches': history[:-1]} if history and history[-1]['worker'] == worker else state
            return worker_state(released, worker, updated), {
                'worker': worker, 'action': 'refused', 'io': out}
        unavailable = verdict.get('evt') in {
            'dispatch_flash_unavailable', 'dispatch_mimo_unavailable'}
        if unavailable and item['kind'] == 'mechanical':
            updated = {**ws, 'active': None, 'last_fingerprint': None,
                       'queue': [{**item, 'assignee': 'efficient'}, *rest]}
        elif unavailable:
            reason = f"{verdict.get('evt', 'dispatch_unavailable').removeprefix('dispatch_')}:{item.get('id', '')}"
            # Keep the packet behind the rest of the batch so other work still
            # runs; never fall back to Efficient for non-mechanical.
            updated, _ = escalation(worker, {**ws, 'active': None, 'last_fingerprint': None,
                                             'queue': [*rest, item]}, reason, run, env)
            if updated.get('notified') != reason:
                updated = {**updated, 'pending_notice': reason}
        else:
            updated = {**ws, 'active': None, 'last_fingerprint': None,
                       'issue': verdict.get('evt', 'dispatch_failed')}
        return worker_state(state, worker, updated), {'worker': worker, 'action': 'refused', 'io': out}
    updated = {**ws, 'queue': rest,
               'active': {**ws['active'], 'status': 'sent' if out.get('ok') else 'uncertain'}}
    return worker_state(state, worker, updated), {'worker': worker, 'action': 'goal', 'io': out}


def progress(worker, entry, snapshot, state, path, client, env, now, run):
    ws = state.get('workers', {}).get(worker, {})
    if snapshot['goal']['state'] == 'PARKED':
        reason = f"parked:{snapshot['goal'].get('park_reason')}:{planning.identity(snapshot)}"
        updated, action = escalation(worker, ws, reason, run, env)
        return worker_state(state, worker, updated), action
    notice_action = None
    if ws.get('pending_notice'):
        updated, notice_action = escalation(worker, ws, ws['pending_notice'], run, env)
        if updated.get('notified') == ws['pending_notice']:
            updated = {**updated, 'pending_notice': None}
        state = worker_state(state, worker, updated)
        ws = updated
    if ws.get('planning'):
        state, action = finish_plan(worker, ws, state, client, env, now, run)
        if action:
            return state, action
        snapshot = client.snapshot(worker)
        ws = state['workers'][worker]
    active = ws.get('active')
    if active:
        if planning.identity(snapshot) == active['origin']:
            if active['status'] == 'prepared' and snapshot['policy'].get('can_dispatch_goal'):
                return send_prepared(worker, snapshot, state, path, client, env, run)
            return state, {'worker': worker, 'action': 'waiting', 'reason': 'dispatch_confirmation'}
        if not snapshot['policy'].get('can_dispatch_goal'):
            return state, {'worker': worker, 'action': 'waiting', 'reason': 'implementer'}
        queue = ws.get('queue') or []
        if active['status'] == 'sending':
            queue = [row for row in queue if row.get('id') != active['packet'].get('id')]
        ws = {**ws, 'active': None, 'queue': queue}
        if active['packet'].get('requires_astra_review') or snapshot['goal']['state'] == 'FAILED':
            ws = {**ws, 'queue': []}
        state = worker_state(state, worker, ws)
    item = next_packet(worker, entry, snapshot, ws)
    authorized = ws.get('queue') or ws.get('awaiting_next') or ws.get('issue') or snapshot['policy'].get('grokbot_may_advance')
    if item and authorized and snapshot['policy'].get('can_dispatch_goal'):
        return execute_packet(worker, item, snapshot, state, path, client, env, now, run)
    if ws.get('retry_after', 0) > now:
        updated, action = escalation(worker, ws, ws.get('issue', 'retry_backoff'), run, env)
        return worker_state(state, worker, updated), action
    if snapshot['policy'].get('grokbot_may_advance'):
        if entry.get('planner'):
            return start_plan(worker, entry, snapshot, state, path, client, env, now, run)
        ws = {**ws, 'awaiting_next': planning.identity(snapshot), 'notified': None,
              'advance_request': f"missing-{planning.identity(snapshot)}"}
        state = worker_state(state, worker, ws)
        write_json(path, state)
    if ws.get('advance_request') and ws['awaiting_next'] == planning.identity(snapshot):
        planning.advance(client, worker, snapshot, ws['advance_request'])
        ws = {**ws, 'advance_request': None}
        state = worker_state(state, worker, ws)
    if ws.get('awaiting_next'):
        updated, action = escalation(worker, ws, f"no_next_goal:{ws['awaiting_next']}", run, env)
        return worker_state(state, worker, updated), action
    if snapshot['policy'].get('idle_slo_violated'):
        updated, action = escalation(worker, ws, f"idle_slo:{planning.identity(snapshot)}", run, env)
        return worker_state(state, worker, updated), action
    return state, notice_action or {'worker': worker, 'action': 'skip'}


def tick(workers, client, path, env, *, now=None, dry=False, run=run_command, load=None):
    if not enabled(env, 'SPECTRE_LOOP'):
        return {'ok': True, 'disabled': True, 'dry_run': dry}
    validate_workers(workers)
    now = time.time() if now is None else now
    if dry:
        return {'ok': True, 'dry_run': True, 'actions': [dry_action(name, entry,
            client.snapshot(name), env, path, now) for name, entry in sorted(workers.items())]}
    with locked(path.with_suffix('.lock')):
        # spectre-kimi commits the seat and rewrites the planner pin under this lock; a
        # registry read before it was taken can pair the new seat with the old pin.
        if load is not None:
            workers = load()
            if not workers:
                raise ValueError('worker registry is empty or unreadable')
            validate_workers(workers)
        return live_tick(workers, client, path, env, now, run)


def live_tick(workers, client, path, env, now, run):
    state = read_json(path)
    actions = []
    deadline = time.monotonic() + TICK_SECONDS
    for worker, entry in sorted(workers.items()):
        if time.monotonic() >= deadline:
            actions = [*actions, {'worker': worker, 'action': 'deferred'}]
            break
        snapshot = client.snapshot(worker)
        policy = snapshot.get('policy', {})
        if snapshot.get('goal', {}).get('state') == 'UNKNOWN' or policy.get('continuity_recovery_allowed'):
            actions = [*actions, {'worker': worker, 'action': 'skip', 'reason': 'occupancy'}]
            continue
        if entry.get('planner') and not enabled(env, 'ASTRA_ENABLED'):
            actions = [*actions, {'worker': worker, 'action': 'skip', 'reason': 'planner_pin'}]
            continue
        state, action = progress(worker, entry, snapshot, state, path, client, env, now, run)
        write_json(path, state)
        actions = [*actions, action]
    return {'ok': all(a.get('io', {}).get('ok', True) for a in actions),
            'dry_run': False, 'actions': actions}
