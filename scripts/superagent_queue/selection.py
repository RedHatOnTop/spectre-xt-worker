"""Route, resume, SOS, and profile decisions over immutable snapshots.

Orca remains the owner of Runs, Tasks, and Dispatches. This module does not
launch a process, open a proxy, or start a second scheduler. Agentrouter and
Anyrouter budgets come from the existing Fedora proxy
``~/.local/bin/codex-agentrouter-portable`` as a caller-supplied snapshot.
"""
import json
import math
from pathlib import Path
import re

from .model import MAX_JSON_BYTES, encoded, identifier, timestamp
from .storage import transaction

ORCHESTRATOR_MODEL = 'gpt-6.1-sol'
ORCHESTRATOR_ENDPOINT = 'agentrouter/anyrouter'
ORCHESTRATOR_ROUTE = 'orca_agentrouter'
SOS_WINDOW_SECONDS = 5 * 60 * 60
SOS_LIMIT = 1
FREE_FAMILIES = frozenset(('cline', 'zen', 'qoder', 'zcode-glm', 'stealth', 'temporary'))
PAID_FAMILIES = frozenset(('clinepass', 'opencode-go'))
CONSUME = frozenset(('implementation', 'agentrouter', 'dsh', 'balanced'))
PRICE_KEYS = ('prompt', 'completion', 'request')
ADAPTER = 'registered'
BLOCKING = frozenset(('live', 'unverifiable'))
MODEL_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:/-]{0,255}')
EVIDENCE_PREFIX = 'evidence: '
TABLES = (
    '''CREATE TABLE IF NOT EXISTS selection_mandate (
        work_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, body TEXT NOT NULL,
        updated_at REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS selection_route (
        route_id TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS selection_profile (
        model_id TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS selection_sos (
        seq INTEGER PRIMARY KEY, episode_id TEXT NOT NULL, work_id TEXT NOT NULL,
        owner TEXT NOT NULL, insight_model TEXT NOT NULL, created_at REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS selection_fault (
        seq INTEGER PRIMARY KEY, body TEXT NOT NULL, created_at REAL NOT NULL)''',
    '''CREATE TABLE IF NOT EXISTS selection_assignment (
        work_id TEXT PRIMARY KEY, owner TEXT NOT NULL, route_id TEXT NOT NULL,
        state TEXT NOT NULL, body TEXT NOT NULL, updated_at REAL NOT NULL)''',
)


def instructions():
    return (
        'Native authority stays with Orca Run, Task, and Dispatch. '
        'The continuity journal resumes the same mandate after a lost response, provider outage, or process restart. '
        'Do not launch a second owner for a live or unverifiable worker. '
        'GPT-6.1 Sol on the direct Agentrouter B profile (orca-agentrouter-b, no local proxy) is the only orchestrator. '
        'Register a harness or a temporary free endpoint as route data with adapter "registered"; do not add a scheduler. '
        'SOS allows one insight for a recorded stuck episode inside five hours, keeps the same owner, and opens no paid route. '
        'A second SOS in that window, or an SOS without a stuck episode, performs no dispatch. '
        'Append unexpected faults to the journal selection_fault record printed by spectre-superagent status. '
        'Do not enable spectre-superagent.timer, reboot, or start a live worker from this command.'
    )


def orchestrator():
    return {'model': ORCHESTRATOR_MODEL, 'endpoint': ORCHESTRATOR_ENDPOINT, 'count': 1,
            'route_id': ORCHESTRATOR_ROUTE}


def model_name(value):
    if (not isinstance(value, str) or not MODEL_NAME.fullmatch(value) or '..' in value
            or '//' in value or value.startswith('/')):
        raise ValueError('model id must be a bounded token')
    return value


def number(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)


def fault_record(journal):
    return f'{journal.path}#selection_fault'


def table_exists(journal, name):
    return journal.connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def ensure(journal):
    with transaction(journal.connection):
        for statement in TABLES:
            journal.connection.execute(statement)


def has_mandate(journal):
    if not table_exists(journal, 'selection_mandate'):
        return False
    return journal.connection.execute('SELECT 1 FROM selection_mandate LIMIT 1').fetchone() is not None


def _rows(journal, name, query):
    if not table_exists(journal, name):
        return []
    return list(journal.connection.execute(query))


def load(journal):
    mandates = {}
    for row in _rows(journal, 'selection_mandate', 'SELECT work_id, body FROM selection_mandate ORDER BY work_id'):
        mandates[row['work_id']] = json.loads(row['body'])
    routes = {}
    for row in _rows(journal, 'selection_route', 'SELECT route_id, body FROM selection_route ORDER BY route_id'):
        routes[row['route_id']] = json.loads(row['body'])
    profiles = {}
    for row in _rows(journal, 'selection_profile', 'SELECT model_id, body FROM selection_profile ORDER BY model_id'):
        profiles[row['model_id']] = row['body']
    sos = [{'episode_id': row['episode_id'], 'work_id': row['work_id'], 'owner': row['owner'],
            'insight_model': row['insight_model'], 'at': row['created_at']}
           for row in _rows(journal, 'selection_sos', 'SELECT * FROM selection_sos ORDER BY seq')]
    assignments = {}
    for row in _rows(journal, 'selection_assignment', 'SELECT work_id, body FROM selection_assignment'):
        assignments[row['work_id']] = json.loads(row['body'])
    claims = journal.connection.execute('''SELECT effect.lane, effect.status, attempt.owner
        FROM effect JOIN attempt ON attempt.token=effect.claim_token
        WHERE effect.status IN ('running','reconciling') AND attempt.finished_at IS NULL
        ORDER BY effect.seq''')
    return {'mandates': mandates, 'routes': routes, 'profiles': profiles, 'sos': sos,
            'assignments': assignments,
            'open_claims': [{'lane': row['lane'], 'status': row['status'], 'owner': row['owner']} for row in claims]}


def secret(text):
    lowered = text.lower()
    markers = ('sk-', 'api_key', 'api key', 'bearer ', 'password', 'private_key', 'begin ')
    return any(marker in lowered for marker in markers)


def profile_from_transcript(model_id, transcript):
    name = model_name(model_id)
    if not isinstance(transcript, list):
        raise ValueError('transcript must be a list of turns')
    facts, seen = [], set()
    for turn in transcript:
        if not isinstance(turn, dict):
            raise ValueError('transcript turn must be an object')
        text = turn.get('text')
        if turn.get('role') not in ('assistant', 'model') or not isinstance(text, str):
            continue
        for line in text.splitlines():
            if not line.startswith(EVIDENCE_PREFIX):
                continue
            fact = line[len(EVIDENCE_PREFIX):].strip()
            if not fact or fact in seen or fact not in text or secret(fact):
                continue
            seen.add(fact)
            facts.append(fact)
    return 'model: ' + name + '\n' + ''.join(f'{EVIDENCE_PREFIX}{fact}\n' for fact in facts)


def update_profile(journal, model_id, transcript, now=None):
    body, stamp = profile_from_transcript(model_id, transcript), timestamp(now)
    if len(body.encode('utf-8')) > MAX_JSON_BYTES:
        raise ValueError('profile text exceeds the byte limit')
    ensure(journal)
    with transaction(journal.connection):
        journal.connection.execute('''INSERT INTO selection_profile VALUES (?,?,?)
            ON CONFLICT(model_id) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at''',
            (model_name(model_id), body, stamp))
    return body


def price_components(price):
    if not isinstance(price, dict) or any(key not in price for key in PRICE_KEYS):
        return None
    values = []
    for key, value in price.items():
        if not isinstance(key, str) or not key or not number(value) or value < 0:
            return None
        values.append(value)
    return values


def zero_price(route):
    values = price_components(route.get('price'))
    return values is not None and all(value == 0 for value in values)


def known_price(route):
    return price_components(route.get('price')) is not None


def unexpired(route, now):
    expires = route.get('expires_at')
    if expires is None:
        return True
    return number(expires) and expires > now


def quota(route):
    if route.get('exhausted') is not False:
        return None
    value = route.get('remaining')
    if not number(value) or value <= 0:
        return None
    return value


def above_bar(route, bar):
    value = route.get('usefulness')
    if not number(value) or value < bar:
        return None
    return value


def stealth_admitted(route, now):
    model = route.get('model')
    if not isinstance(model, str) or not model.startswith('stealth/') or model.endswith(':free'):
        return False
    if route.get('malformed') is not False:
        return False
    return zero_price(route) and unexpired(route, now)


def eligible_free(route, bar, now):
    if route.get('role') != 'worker' or route.get('adapter') != ADAPTER:
        return False
    if route.get('family') not in FREE_FAMILIES or route.get('malformed') is not False:
        return False
    if above_bar(route, bar) is None or quota(route) is None:
        return False
    if route['family'] == 'stealth':
        return stealth_admitted(route, now)
    return zero_price(route) and unexpired(route, now)


def eligible_paid(route, family):
    if route.get('role') != 'worker' or route.get('adapter') != ADAPTER or route.get('family') != family:
        return False
    if route.get('approved_paid') is not True or route.get('malformed') is not False:
        return False
    if quota(route) is None or not known_price(route):
        return False
    return True


def family_budget(budgets, family):
    row = budgets.get(family) if isinstance(budgets, dict) else None
    if not isinstance(row, dict) or row.get('exhausted') is True:
        return {'exhausted': True, 'known': False, 'remaining': 0}
    remaining = row.get('remaining')
    if not number(remaining) or remaining < 0:
        return {'exhausted': True, 'known': False, 'remaining': 0}
    limit = row.get('limit', remaining)
    if not number(limit) or limit <= 0:
        limit = remaining if remaining > 0 else 1
    return {'exhausted': remaining <= 0, 'known': True, 'remaining': remaining, 'limit': limit}


def budgets_from_proxy(health, dsh):
    """Map an existing proxy health snapshot. This does not contact the proxy."""
    if not isinstance(health, dict) or not isinstance(health.get('providers'), dict):
        raise ValueError('agentrouter health snapshot must name its providers')
    if not isinstance(dsh, dict):
        raise ValueError('dsh budget snapshot must be an object')
    remaining, open_provider = 0, False
    for name in ('agentrouter', 'anyrouter'):
        row = health['providers'].get(name)
        if row is None:
            continue
        if not isinstance(row, dict) or row.get('exhausted') is True:
            continue
        value = row.get('remaining')
        if not number(value) or value < 0:
            continue
        open_provider = True
        remaining += value
    present = any(name in health['providers'] for name in ('agentrouter', 'anyrouter'))
    agentrouter = {'remaining': remaining, 'exhausted': not open_provider,
                   'limit': remaining if remaining > 0 else 1, 'known': present and open_provider}
    if not present:
        agentrouter = {'remaining': 0, 'exhausted': True, 'limit': 1, 'known': False}
    return {'agentrouter': agentrouter, 'dsh': {
        'remaining': dsh.get('remaining', 0), 'exhausted': dsh.get('exhausted', True),
        'limit': dsh.get('limit', dsh.get('remaining', 0))}}


def richer_family(budgets):
    ranked = []
    for name in ('agentrouter', 'dsh'):
        budget = family_budget(budgets, name)
        if budget['exhausted'] or not budget['known']:
            continue
        ranked.append((budget['remaining'] / budget['limit'], budget['remaining'], name))
    if not ranked:
        return None
    ranked.sort()
    return ranked[-1][2]


def blocking_owners(snapshot, catalog, work):
    owners = []
    for row in snapshot.get('workers') or []:
        if not isinstance(row, dict):
            raise ValueError('worker snapshot rows must be objects')
        same = row.get('work_id') == work['work_id'] or row.get('run_id') == work['run_id']
        if not same or row.get('state') not in BLOCKING:
            continue
        owner = row.get('owner')
        if not isinstance(owner, str) or not owner:
            owners.append('unverifiable')
            continue
        identifier(owner)
        owners.append(owner)
    for claim in catalog.get('open_claims', []):
        if claim['lane'] == work['run_id']:
            identifier(claim['owner'])
            owners.append(claim['owner'])
    return list(dict.fromkeys(owners))


def native_worker_states(data, run_id, work_id):
    if not isinstance(data, dict):
        raise ValueError('native inventory must be an object')
    rows = []
    for row in data.get('workers') or []:
        if not isinstance(row, dict) or row.get('runId') != run_id:
            continue
        liveness = ((row.get('projection') or {}).get('liveness') or {}).get('verdict')
        released = row.get('terminalState') == 'released' and (row.get('resource') or {}).get('releaseState') == 'released'
        if liveness == 'unverifiable':
            state = 'unverifiable'
        elif released:
            continue
        elif row.get('terminalState') == 'active' or row.get('dispatchStatus') == 'dispatched':
            state = 'live'
        else:
            continue
        owner = row.get('dispatchId') or row.get('agentTerminalHandle') or 'unverifiable'
        rows.append({'work_id': work_id, 'run_id': run_id, 'state': state, 'owner': owner})
    return rows


def base_decision(work, **fields):
    decision = {'action': 'refused', 'work_id': work['work_id'], 'run_id': work['run_id'],
                'owner': None, 'launch': False, 'native_start': False, 'second_owner': False,
                'family': None, 'route_id': None, 'model': None, 'profile_text': '',
                'paid_route_opened': False, 'ownership_transferred': False, 'dispatch': False,
                'reason': '', 'insight': '', 'insight_model': None, 'orchestrator': orchestrator()}
    return {**decision, **fields}


def refused(reason, work=None):
    identity = work or {'work_id': None, 'run_id': None}
    return base_decision(identity, action='refused', reason=reason)


def work_of(snapshot):
    if not isinstance(snapshot, dict):
        raise ValueError('selection snapshot must be an object')
    work = snapshot.get('work')
    if not isinstance(work, dict):
        raise ValueError('selection work must be an object')
    consume = work.get('consume', 'implementation')
    suitable = work.get('suitable_for_free', False)
    if consume not in CONSUME:
        raise ValueError('selection consume must name an approved family')
    if suitable is not True and suitable is not False:
        raise ValueError('selection suitability must be explicit')
    if work.get('authorized') is not True and work.get('authorized') is not False:
        raise ValueError('selection work authorization must be explicit')
    return {'work_id': identifier(work.get('work_id')), 'run_id': identifier(work.get('run_id')),
            'authorized': work['authorized'], 'consume': consume, 'suitable_for_free': suitable}


def usefulness_bar(snapshot):
    value = snapshot.get('usefulness_bar')
    if not number(value) or not 0 <= value <= 1:
        raise ValueError('usefulness bar must be a recorded number from 0 to 1')
    return value


def mandate_matches(catalog, work):
    row = catalog['mandates'].get(work['work_id'])
    return bool(row and row.get('run_id') == work['run_id'] and row.get('authorized') is True)


def profile_text(catalog, model):
    if model is None:
        return ''
    return catalog['profiles'].get(model, '')


def choose_free(catalog, bar, now):
    ranked = []
    for route in catalog['routes'].values():
        if not eligible_free(route, bar, now):
            continue
        ranked.append((above_bar(route, bar), quota(route), route['route_id'], route))
    if not ranked:
        return None
    ranked.sort(key=lambda item: (-item[0], -item[1], item[2]))
    return ranked[0][3]


def choose_paid(catalog, family):
    ranked = [route for route in catalog['routes'].values() if eligible_paid(route, family)]
    if not ranked:
        return None
    ranked.sort(key=lambda route: route['route_id'])
    return ranked[0]


def selected(work, route, catalog, family):
    return base_decision(
        work, action='select', owner=f"worker:{route['route_id']}", launch=True, family=family,
        route_id=route['route_id'], model=route['model'], profile_text=profile_text(catalog, route['model']),
        reason='selected')


def assign_orchestrator(work, catalog, budgets):
    budget = family_budget(budgets, 'agentrouter')
    if budget['exhausted'] or not budget['known']:
        return refused('agentrouter_exhausted', work)
    return base_decision(
        work, action='select', owner='orchestrator:gpt-6.1-sol', launch=False, family='agentrouter',
        route_id=ORCHESTRATOR_ROUTE, model=ORCHESTRATOR_MODEL,
        profile_text=profile_text(catalog, ORCHESTRATOR_MODEL), reason='orchestrator')


def assign_dsh(work, catalog, budgets):
    if family_budget(budgets, 'dsh')['exhausted']:
        return refused('dsh_exhausted', work)
    route = choose_paid(catalog, 'clinepass') or choose_paid(catalog, 'opencode-go')
    if route is None:
        return refused('no_approved_paid_route', work)
    return selected(work, route, catalog, 'dsh')


def sos_decision(catalog, snapshot, work, now):
    sos = snapshot.get('sos')
    if not isinstance(sos, dict):
        raise ValueError('sos request must be an object')
    owner = sos.get('owner')
    if sos.get('stuck') is not True or sos.get('recorded') is not True:
        return base_decision(work, action='sos', owner=owner if isinstance(owner, str) else None,
                             reason='no_stuck_episode')
    episode, stuck_model = identifier(sos.get('episode_id')), sos.get('model')
    if stuck_model is not None:
        model_name(stuck_model)
    identifier(owner)
    if not mandate_matches(catalog, work):
        return refused('no_mandate', work)
    prior = [row for row in catalog['sos']
             if row['episode_id'] == episode and now - row['at'] < SOS_WINDOW_SECONDS]
    if len(prior) >= SOS_LIMIT:
        return base_decision(work, action='sos', owner=owner, reason='sos_limit')
    others = sorted(model for model in catalog['profiles'] if model != stuck_model)
    if not others:
        return base_decision(work, action='sos', owner=owner, reason='no_other_profile')
    insight_model = others[0]
    return {**base_decision(
        work, action='sos', owner=owner, dispatch=True, reason='sos', insight_model=insight_model,
        insight=catalog['profiles'][insight_model], profile_text=catalog['profiles'][insight_model]),
        'episode_id': episode}


def decide(catalog, snapshot, now=None):
    stamp = timestamp(now if now is not None else snapshot.get('now') if isinstance(snapshot, dict) else None)
    work = work_of(snapshot)
    if snapshot.get('sos') is not None:
        return sos_decision(catalog, snapshot, work, stamp)
    if not work['authorized'] or not mandate_matches(catalog, work):
        return refused('no_mandate' if work['authorized'] else 'unauthorized', work)
    owners = blocking_owners(snapshot, catalog, work)
    if owners:
        return base_decision(work, action='retain', owner=owners[0], reason='live_or_unverifiable')
    budgets = snapshot.get('budgets') if isinstance(snapshot.get('budgets'), dict) else {}
    if work['consume'] == 'agentrouter':
        return assign_orchestrator(work, catalog, budgets)
    if work['suitable_for_free']:
        route = choose_free(catalog, usefulness_bar(snapshot), stamp)
        if route is not None:
            return selected(work, route, catalog, route['family'])
    if work['consume'] == 'balanced':
        family = richer_family(budgets)
        if family is None:
            return refused('subscription_exhausted', work)
        if family == 'agentrouter':
            return assign_orchestrator(work, catalog, budgets)
        return assign_dsh(work, catalog, budgets)
    return assign_dsh(work, catalog, budgets)


def normalize_route(route, now):
    if not isinstance(route, dict):
        raise ValueError('route registration must be an object')
    if set(route) != {'route_id', 'harness', 'adapter', 'family', 'model', 'role', 'price',
                      'expires_at', 'usefulness', 'remaining', 'approved_paid', 'malformed', 'exhausted'}:
        raise ValueError('route registration has unknown or missing fields')
    if secret(json.dumps(route, ensure_ascii=False)):
        raise ValueError('route registration cannot carry a secret')
    if route['role'] != 'worker' or route['adapter'] != ADAPTER:
        raise ValueError('a registered harness is worker data with the registered adapter')
    family = route['family']
    if family not in FREE_FAMILIES and family not in PAID_FAMILIES:
        raise ValueError('route family is not an admitted worker family')
    if route['approved_paid'] is not True and route['approved_paid'] is not False:
        raise ValueError('route paid approval must be explicit')
    if route['malformed'] is not True and route['malformed'] is not False:
        raise ValueError('route malformed flag must be explicit')
    if route['exhausted'] is not True and route['exhausted'] is not False:
        raise ValueError('route exhaustion flag must be explicit')
    if family in PAID_FAMILIES and route['approved_paid'] is not True:
        raise ValueError('a paid route requires recorded approval')
    if family in FREE_FAMILIES and route['approved_paid'] is not False:
        raise ValueError('a free route is not an approved paid fallback')
    expires = route['expires_at']
    if expires is not None and not number(expires):
        raise ValueError('route expiry must be a finite timestamp or empty')
    remaining = route['remaining']
    if remaining is not None and not number(remaining):
        raise ValueError('route remaining quota must be numeric or empty')
    usefulness = route['usefulness']
    if usefulness is not None and not number(usefulness):
        raise ValueError('route usefulness must be numeric or empty')
    identifier(route['harness'])
    body = {'route_id': identifier(route['route_id']), 'harness': route['harness'], 'adapter': ADAPTER,
            'family': family, 'model': model_name(route['model']), 'role': 'worker', 'price': route['price'],
            'expires_at': expires, 'usefulness': usefulness, 'remaining': remaining,
            'approved_paid': route['approved_paid'], 'malformed': route['malformed'],
            'exhausted': route['exhausted']}
    if body['model'] == ORCHESTRATOR_MODEL:
        raise ValueError('orchestrator identity is fixed')
    encoded({'registered_at': now, **{key: value for key, value in body.items() if key != 'price'},
             'price': body['price'] if isinstance(body['price'], dict) else {'unknown': True}})
    return body


def register_route(journal, route, now=None):
    stamp = timestamp(now)
    body = normalize_route(route, stamp)
    ensure(journal)
    encoded_body = encoded(body)
    with transaction(journal.connection):
        current = journal.connection.execute('SELECT body FROM selection_route WHERE route_id=?',
                                            (body['route_id'],)).fetchone()
        if current is None:
            journal.connection.execute('INSERT INTO selection_route VALUES (?,?,?)',
                                       (body['route_id'], encoded_body, stamp))
            stored, conflict = body, False
        elif current['body'] == encoded_body:
            stored, conflict = json.loads(current['body']), False
        else:
            stored, conflict = None, True
    if conflict:
        append_fault(journal, {'kind': 'register', 'error': 'route identity conflict',
                               'route_id': body['route_id']}, now=stamp)
        raise ValueError('route identity conflict')
    return stored


def bind_mandate(journal, mandate, now=None):
    if not isinstance(mandate, dict) or set(mandate) - {'work_id', 'run_id', 'authorized'}:
        raise ValueError('mandate fields are work_id, run_id, and authorized')
    if mandate.get('authorized') is not True:
        raise ValueError('only an authorized mandate is retained')
    body = {'work_id': identifier(mandate.get('work_id')), 'run_id': identifier(mandate.get('run_id')),
            'authorized': True}
    stamp = timestamp(now)
    ensure(journal)
    with transaction(journal.connection):
        current = journal.connection.execute('SELECT body FROM selection_mandate WHERE work_id=?',
                                            (body['work_id'],)).fetchone()
        encoded_body = encoded(body)
        if current and current['body'] != encoded_body:
            raise ValueError('mandate identity conflict')
        journal.connection.execute('''INSERT INTO selection_mandate VALUES (?,?,?,?)
            ON CONFLICT(work_id) DO UPDATE SET run_id=excluded.run_id, body=excluded.body,
            updated_at=excluded.updated_at''', (body['work_id'], body['run_id'], encoded_body, stamp))
    return body


def append_fault(journal, body, now=None):
    if not isinstance(body, dict):
        body = {'error': 'fault body was not an object'}
    stamp, payload = timestamp(now), encoded(body)
    ensure(journal)
    with transaction(journal.connection):
        seq = journal.connection.execute('SELECT COALESCE(MAX(seq),0)+1 FROM selection_fault').fetchone()[0]
        journal.connection.execute('INSERT INTO selection_fault VALUES (?,?,?)', (seq, payload, stamp))
    return fault_record(journal)


def record(journal, decision, now=None):
    stamp = timestamp(now)
    ensure(journal)
    action = decision.get('action')
    with transaction(journal.connection):
        if action == 'sos':
            if decision.get('dispatch') is True:
                seq = journal.connection.execute('SELECT COALESCE(MAX(seq),0)+1 FROM selection_sos').fetchone()[0]
                journal.connection.execute('INSERT INTO selection_sos VALUES (?,?,?,?,?,?)',
                    (seq, identifier(decision['episode_id']), identifier(decision['work_id']),
                     identifier(decision['owner']), model_name(decision['insight_model']), stamp))
            return decision
        if action not in ('select', 'retain'):
            return decision
        work_id = identifier(decision['work_id'])
        current = journal.connection.execute('SELECT body FROM selection_assignment WHERE work_id=?',
                                            (work_id,)).fetchone()
        existing = json.loads(current['body']) if current else None
        if existing and action == 'retain':
            decision = {**decision, 'owner': existing['owner'], 'route_id': existing.get('route_id') or None,
                        'second_owner': False, 'launch': False}
        stored = {'work_id': work_id, 'owner': decision['owner'], 'route_id': decision.get('route_id') or '',
                  'state': action, 'run_id': decision['run_id']}
        payload = encoded(stored)
        journal.connection.execute('''INSERT INTO selection_assignment VALUES (?,?,?,?,?,?)
            ON CONFLICT(work_id) DO UPDATE SET owner=excluded.owner, route_id=excluded.route_id,
            state=excluded.state, body=excluded.body, updated_at=excluded.updated_at''',
            (work_id, stored['owner'], stored['route_id'], action, payload, stamp))
    return decision


def status_view(journal):
    catalog = load(journal)
    work_ids = sorted(catalog['mandates'])
    routes = [{'route_id': route['route_id'], 'family': route['family'], 'model': route['model'],
               'harness': route['harness']} for route in (catalog['routes'][key] for key in sorted(catalog['routes']))]
    faults = 0
    if table_exists(journal, 'selection_fault'):
        faults = journal.connection.execute('SELECT COUNT(*) FROM selection_fault').fetchone()[0]
    return {'work_id': work_ids[0] if len(work_ids) == 1 else None, 'work_ids': work_ids,
            'orchestrator': orchestrator(), 'admitted_routes': routes,
            'fault_record': fault_record(journal), 'faults': faults}


def load_ledger(directory):
    from .settings import read_private
    path = Path(directory) / 'free-ledger.json'
    if not path.exists():
        return None
    value = read_private(path)
    if not isinstance(value, dict):
        raise ValueError('free ledger must be an object')
    return value


def apply_ledger(journal, ledger, now=None):
    stamp = timestamp(now)
    for mandate in ledger.get('mandates') or []:
        bind_mandate(journal, mandate, now=stamp)
    for route in ledger.get('routes') or []:
        try:
            register_route(journal, route, now=stamp)
        except ValueError as exc:
            append_fault(journal, {'kind': 'register', 'error': str(exc),
                                   'route_id': route.get('route_id') if isinstance(route, dict) else None}, now=stamp)
    transcripts = ledger.get('transcripts') or {}
    if not isinstance(transcripts, dict):
        raise ValueError('free ledger transcripts must be an object')
    for model, transcript in transcripts.items():
        update_profile(journal, model, transcript, now=stamp)
    return load(journal)


def snapshot_from_ledger(ledger, data, catalog, now):
    work = ledger.get('work')
    if not isinstance(work, dict):
        raise ValueError('free ledger work must be an object')
    health, dsh = ledger.get('proxy'), ledger.get('dsh') or {'remaining': 0, 'exhausted': True, 'limit': 1}
    budgets = budgets_from_proxy(health, dsh) if isinstance(health, dict) else {
        'agentrouter': {'remaining': 0, 'exhausted': True, 'limit': 1},
        'dsh': dsh if isinstance(dsh, dict) else {'remaining': 0, 'exhausted': True, 'limit': 1}}
    workers = []
    for mandate in catalog['mandates'].values():
        workers.extend(native_worker_states(data if isinstance(data, dict) else {}, mandate['run_id'], mandate['work_id']))
    snapshot = {'now': now, 'usefulness_bar': ledger.get('usefulness_bar', 0.5), 'work': work,
                'workers': workers, 'budgets': budgets}
    if ledger.get('sos') is not None:
        snapshot['sos'] = ledger['sos']
    return snapshot


def choose_for_tick(journal, data, now=None):
    """Read the journal-local ledger and record one decision. Does not launch."""
    stamp = timestamp(now)
    try:
        ledger = load_ledger(journal.path.parent)
    except (OSError, ValueError) as exc:
        append_fault(journal, {'kind': 'ledger', 'error': str(exc)}, now=stamp)
        return None
    if ledger is None:
        return retain_observed(journal, data, stamp)
    if not ledger.get('work') and not ledger.get('mandates'):
        return None
    try:
        catalog = apply_ledger(journal, ledger, stamp)
        if not ledger.get('work'):
            return None
        decision = decide(catalog, snapshot_from_ledger(ledger, data, catalog, stamp), now=stamp)
        decision = record(journal, decision, now=stamp)
    except (OSError, ValueError) as exc:
        append_fault(journal, {'kind': 'choose', 'error': str(exc)}, now=stamp)
        return refused(str(exc))
    work = ledger['work']
    return {**decision, 'task_id': work.get('task_id'), 'worktree_id': work.get('worktree_id')}


def retain_observed(journal, data, now=None):
    """Keep a live native owner even when no ledger or selection row exists yet."""
    retained = retain_if_blocked(journal, data, now)
    if retained is not None or has_mandate(journal):
        return retained
    if not isinstance(data, dict):
        return None
    stamp = timestamp(now)
    result = None
    runs = dict.fromkeys(row.get('runId') for row in data.get('workers') or [] if isinstance(row, dict))
    for run_id in runs:
        if not isinstance(run_id, str):
            continue
        try:
            identifier(run_id)
            workers = native_worker_states(data, run_id, run_id)
            work = {'work_id': run_id, 'run_id': run_id, 'authorized': True,
                    'consume': 'implementation', 'suitable_for_free': False}
            owners = blocking_owners({'workers': workers}, {'open_claims': []}, work)
        except ValueError:
            continue
        if not owners:
            continue
        result = record(journal, base_decision(work, action='retain', owner=owners[0],
                                               reason='live_or_unverifiable'), now=stamp)
    return result


def retain_if_blocked(journal, data, now=None):
    if not has_mandate(journal):
        return None
    stamp = timestamp(now)
    try:
        catalog = load(journal)
    except ValueError as exc:
        append_fault(journal, {'kind': 'retain', 'error': str(exc)}, now=stamp)
        return None
    retained = None
    for work_id, mandate in sorted(catalog['mandates'].items()):
        work = {'work_id': work_id, 'run_id': mandate['run_id'], 'authorized': True,
                'consume': 'implementation', 'suitable_for_free': False}
        try:
            workers = native_worker_states(data, mandate['run_id'], work_id)
            owners = blocking_owners({'workers': workers}, catalog, work)
        except ValueError as exc:
            append_fault(journal, {'kind': 'retain', 'error': str(exc), 'work_id': work_id}, now=stamp)
            return retained
        if not owners:
            continue
        decision = base_decision(work, action='retain', owner=owners[0], reason='live_or_unverifiable')
        retained = record(journal, decision, now=stamp)
        catalog = load(journal)
    return retained
