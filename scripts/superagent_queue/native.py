"""Deliver only to the existing native coordinator; never place another owner."""
import json
import subprocess

from orca_runtime import observe
from .model import identifier
from .target import owner, prompt_receipt, validate_payload
from . import reconcile
from . import continuation
from .disposition import effective
from .model import timestamp

WAIT_SUBMIT_SECONDS = 15
SEND_TIMEOUT_SECONDS = 45


def validate_target(payload, data):
    return owner(payload, data).idle


def reconcile_delivery(journal, claim, payload, attempt, data, now):
    prior = attempt['receipt']
    evidence = reconcile.started(payload, prior or {}, data, attempt['started_at'], now=now)
    if evidence is None:
        return None
    receipt = {**prior, 'delivery_evidence': evidence}
    journal.complete(claim, receipt, now=now)
    return {'status': 'delivered', 'receipt': receipt}


def refresh(journal, claim, cli, data, snapshot, request, dispositions, now):
    automatic = continuation.original(claim, journal.claim_event(claim))
    decision = effective(journal, dispositions or {}).get(claim.payload['run_id'], {})
    workers = automatic is not None and not request and not continuation.held(
        claim.payload, {'workers': []}, decision, timestamp(now))
    current = (observe.collect(cli, run_ids=(claim.payload['run_id'],), include_workers=workers)
               if snapshot is not None or automatic is not None else data)
    decision = effective(journal, dispositions or {}).get(claim.payload['run_id'], {})
    if automatic is not None and not request and not workers and not continuation.held(
            claim.payload, {'workers': []}, decision, timestamp(now)):
        current = {**current, 'workers': observe.collect_workers(cli, run_id=claim.payload['run_id'])}
    return automatic, current, validate_target(claim.payload, current)


def guard(journal, claim, automatic, current, request, dispositions, now):
    if journal.supersede_reviewed(claim, now=now):
        return {'status': 'superseded_by_review', 'delivery_sent': False}
    if request:
        return None
    if not journal.prepare_send(claim, now=now):
        return {'status': 'stale_claim', 'reason': 'effect changed before native input'}
    if journal.supersede_continuation(claim, current, dispositions or {}, now=now):
        return {'status': 'superseded_continuation', 'delivery_sent': False}
    decision = effective(journal, dispositions or {}).get(claim.payload['run_id'], {})
    event = journal.claim_event(claim)
    if event and event['source'] == 'mail-obligation' and continuation.provider_wait(decision, timestamp(now)):
        journal.defer(claim, now=now)
        return {'status': 'deferred', 'reason': 'current provider retry holds automatic mail followup'}
    if automatic is not None and not continuation.eligible(claim, automatic, current, decision, timestamp(now)):
        journal.defer(claim, now=now)
        return {'status': 'deferred', 'reason': 'current native facts hold automatic continuation'}
    return None


def deliver(journal, claim, cli, now=None, snapshot=None, dispositions=None):
    payload = validate_payload(claim.payload)
    if claim.kind != 'wake':
        raise ValueError('native wake adapter cannot execute other effect kinds')
    row = journal.claim_record(claim)
    if not row or row['status'] not in ('running', 'reconciling'):
        return {'status': 'stale_claim', 'reason': 'effect no longer owns this operational claim'}
    prior = row['receipt']
    request = prompt_receipt(prior or {}).get('requestId')
    if row['status'] == 'reconciling' and not request:
        return {'status': 'reconciling', 'reason': 'native request identity unavailable; inspect exact original pane'}
    try:
        data = (observe.collect(cli, run_ids=(payload['run_id'],), include_workers=False)
                if snapshot is None else snapshot)
        idle = validate_target(payload, data)
    except ValueError as exc:
        journal.attention(claim, str(exc), now=now)
        return {'status': 'attention', 'reason': str(exc)}
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        if row['status'] != 'reconciling':
            journal.retry(claim, str(exc), now=now)
        return {'status': 'reconciling' if row['status'] == 'reconciling' else 'retry_wait', 'reason': str(exc)}
    if request:
        reconciled = reconcile_delivery(journal, claim, payload, row, data, now)
        if reconciled is not None:
            return reconciled
    if not idle and not request:
        journal.defer(claim, now=now)
        return {'status': 'deferred', 'reason': 'native coordinator is not positively idle'}
    try:
        automatic, current, ready = refresh(journal, claim, cli, data, snapshot, request, dispositions, now)
    except ValueError as exc:
        journal.attention(claim, str(exc), now=now)
        return {'status': 'attention', 'reason': str(exc)}
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        if row['status'] != 'reconciling':
            journal.retry(claim, str(exc), now=now)
        return {'status': 'reconciling' if row['status'] == 'reconciling' else 'retry_wait', 'reason': str(exc)}
    if not ready and not request:
        journal.defer(claim, now=now)
        return {'status': 'deferred', 'reason': 'native coordinator changed state before input'}
    stopped = guard(journal, claim, automatic, current, request, dispositions, now)
    if stopped is not None:
        return stopped
    command = [cli, 'terminal', 'send', '--terminal', payload['terminal_handle'], '--text', payload['text'],
               '--enter', '--wait-submit', str(WAIT_SUBMIT_SECONDS), '--json']
    if request:
        command = [*command, '--retry-request', identifier(request)]
    report = send(journal, claim, payload, command, now)
    return observe_submission(journal, claim, payload, report, cli, now)


def observe_submission(journal, claim, payload, report, cli, now):
    if report['status'] != 'reconciling' or not prompt_receipt(report.get('receipt', {})).get('requestId'):
        return report
    try:
        observed = observe.collect(cli, run_ids=(payload['run_id'],), include_workers=False)
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return {**report, 'observation_error': str(exc)}
    attempt = journal.claim_record(claim)
    return reconcile_delivery(journal, claim, payload, attempt, observed, now) or report


def send(journal, claim, target, command, now):
    result = None
    try:
        result = subprocess.run(command, text=True, capture_output=True, timeout=SEND_TIMEOUT_SECONDS, check=False)
        response = json.loads(result.stdout)
        if not isinstance(response, dict):
            raise ValueError('native response is not an object')
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        output = f'\n{result.stdout}{result.stderr}' if result is not None else ''
        journal.uncertain(claim, f'{exc}{output}', now=now)
        return {'status': 'reconciling', 'reason': f'{exc}{output}'}
    prompt = prompt_receipt(response)
    result_object = response.get('result')
    sent = result_object.get('send') if isinstance(result_object, dict) else None
    matching = (isinstance(sent, dict) and sent.get('handle') == target['terminal_handle']
                and sent.get('accepted') is True and prompt.get('processIncarnation') == target['incarnation_id'])
    expected = command[command.index('--retry-request') + 1] if '--retry-request' in command else None
    request = prompt.get('requestId')
    matching = matching and isinstance(request, str) and bool(request) and (expected is None or request == expected)
    if not matching:
        error = f'native receipt identity mismatch; exit {result.returncode}\n{result.stdout}{result.stderr}'
        journal.uncertain(claim, error, now=now)
        return {'status': 'reconciling', 'reason': error}
    journal.record_receipt(claim, response)
    stages = prompt.get('stages')
    started = isinstance(stages, list) and 'turn_started' in stages
    if result.returncode == 0 and response.get('ok') is True and matching and started:
        journal.complete(claim, response, now=now)
        return {'status': 'delivered', 'receipt': response}
    journal.uncertain(claim, f'native submission unproven; exit {result.returncode}\n{result.stdout}{result.stderr}', now=now)
    return {'status': 'reconciling', 'receipt': response}
