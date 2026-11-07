"""Observe coordinator review receipts without replying, acknowledging or changing ownership."""
import hashlib
import json

from orca_runtime import observe
from .model import encoded, identifier
from .target import owner
from .sources import native_message

MAX_DELIVERY_MESSAGES = 50
ACTIONS = frozenset(('reviewed', 'answered', 'released', 'retained', 'reused', 'rejected'))


def request(cli, request_id, method):
    identifier(request_id)
    row = observe.query(cli, ['orchestration', 'request-show', '--request', request_id])
    receipt = row.get('receipt')
    if (row.get('requestId') != request_id or row.get('state') != 'completed'
            or row.get('method') != method or not isinstance(receipt, dict)):
        raise ValueError('native mail review requires the exact completed mutation receipt')
    return receipt


def validate(data):
    encoded(data)
    if set(data) != {'check_request_id', 'ack_request_id', 'reviews'}:
        raise ValueError('native mail review requires exact check, acknowledgment and review fields')
    identifier(data['check_request_id'])
    identifier(data['ack_request_id'])
    rows = data['reviews']
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_DELIVERY_MESSAGES:
        raise ValueError('native mail review must cover the whole native Delivery')
    for row in rows:
        if (not isinstance(row, dict) or not {'message_id', 'action'} <= set(row)
                or set(row) - {'message_id', 'action', 'request_id', 'dispatch_id'}
                or row['action'] not in ACTIONS):
            raise ValueError('native mail review has invalid action or fields')
        identifier(row['message_id'])
        for field in ('request_id', 'dispatch_id'):
            if field in row:
                identifier(row[field])
    if len({row['message_id'] for row in rows}) != len(rows):
        raise ValueError('native mail review must cover the whole native Delivery exactly once')
    return rows


def completion(cli, message, review, run_id):
    payload = json.loads(message.get('payload') or '{}')
    dispatch_id = identifier(payload.get('dispatchId'))
    task_id = identifier(payload.get('taskId'))
    row = observe.query(cli, ['orchestration', 'worker-show', '--dispatch', dispatch_id])
    dispatch = row.get('dispatch') or {}
    outcome = payload.get('outcome')
    expected = 'completed' if outcome == 'succeeded' else 'failed' if outcome == 'failed' else None
    if (not expected or dispatch.get('id') != dispatch_id or dispatch.get('status') != expected
            or (dispatch.get('runId') or dispatch.get('run_id')) != run_id
            or (dispatch.get('taskId') or dispatch.get('task_id')) != task_id):
        raise ValueError('native worker completion does not match its accepted Task and Dispatch outcome')
    method = {'released': 'orchestration.workerRelease', 'retained': 'orchestration.workerRetain',
              'reused': 'orchestration.workerStart'}.get(review['action'])
    if method is None or 'request_id' not in review:
        raise ValueError('worker completion review requires an actual ownership decision receipt')
    receipt = request(cli, review['request_id'], method)
    if review['action'] == 'reused':
        resource = row.get('terminalResource') or {}
        if (receipt.get('dispatchId') != review.get('dispatch_id') or receipt.get('state') != 'ready'
                or resource.get('ownerDispatchId') != review.get('dispatch_id')):
            raise ValueError('native reuse receipt does not own the original terminal resource')
        return
    expected_states = ('released', 'already_released', 'retained') if review['action'] == 'released' else ('retained',)
    if receipt.get('dispatchId') != dispatch_id or receipt.get('state') not in expected_states:
        raise ValueError('native cleanup receipt does not match its settled Dispatch')


def inspect_message(cli, message, review, run_id):
    if message.get('type') == 'question':
        if review['action'] != 'answered' or 'request_id' not in review:
            raise ValueError('native question review requires an actual reply receipt')
        receipt = request(cli, review['request_id'], 'orchestration.reply')
        question, reply = receipt.get('question') or {}, receipt.get('message') or {}
        if ((question.get('message_id') != message['id'] or question.get('run_id') != run_id)
                and reply.get('thread_id') != message['id']) or not reply.get('body'):
            raise ValueError('native reply receipt does not answer this exact question')
        return
    if message.get('type') == 'worker_done':
        if review['action'] == 'rejected':
            payload = json.loads(message.get('payload') or '{}')
            rejection = payload.get('_orcaLifecycleRejection')
            if (not isinstance(rejection, dict) or not isinstance(rejection.get('code'), str)
                    or not isinstance(rejection.get('reason'), str)):
                raise ValueError('completion rejection requires native rejection evidence')
            return
        completion(cli, message, review, run_id)
        return
    if review['action'] != 'reviewed':
        raise ValueError('ordinary native context requires an explicit reviewed decision')


def original_message(journal, run_id, message):
    current = native_message(message)
    row = journal.connection.execute('''SELECT event.content FROM event
        JOIN effect ON effect.key=event.effect_key WHERE event.source='orca-inbox'
        AND event.event_id=? AND effect.lane=?''', (current['id'], run_id)).fetchone()
    if row is None or encoded(native_message(json.loads(row['content']))) != encoded(current):
        raise ValueError('native review no longer matches its original durable native message')


def publish(journal, target, data, caller, inventory, cli, now=None):
    current = owner(target, inventory)
    if (caller.get('ORCA_TERMINAL_HANDLE') != target['terminal_handle']
            or caller.get('ORCA_PANE_KEY') != current.pane_key):
        raise ValueError('native mail review requires the exact current owner pane')
    reviews = validate(data)
    checked = request(cli, data['check_request_id'], 'orchestration.check')
    acked = request(cli, data['ack_request_id'], 'orchestration.check')
    messages = checked.get('messages')
    delivery_id = checked.get('deliveryId')
    if (checked.get('runId') != target['run_id'] or not isinstance(messages, list)
            or not isinstance(delivery_id, str) or not delivery_id):
        raise ValueError('native mail review requires an issued whole native Delivery')
    if acked.get('runId') != target['run_id'] or acked.get('acknowledged') != delivery_id:
        raise ValueError('native mail review lacks acknowledgment of the exact whole Delivery')
    for message in messages:
        native_message(message)
    if {row.get('id') for row in messages} != {row['message_id'] for row in reviews} or len(messages) != len(reviews):
        raise ValueError('native mail review must cover the whole native Delivery exactly once')
    by_id = {row['message_id']: row for row in reviews}
    for message in messages:
        original_message(journal, target['run_id'], message)
        inspect_message(cli, message, by_id[message['id']], target['run_id'])
    proof = {'check_request_id': data['check_request_id'], 'ack_request_id': data['ack_request_id'],
             'delivery_id': delivery_id, 'receipt_sha256': hashlib.sha256(encoded(checked).encode()).hexdigest(),
             'owner': {'terminal_handle': target['terminal_handle'], 'pane_key': current.pane_key,
                       'incarnation_id': target['incarnation_id']}}
    return journal.record_mail_review(target['run_id'], reviews, proof, now=now)


def instructions():
    return ('Read the original native FIFO Delivery; answer questions and review exact worker evidence. '
            'Decide every settled terminal owner, then acknowledge only the processed whole Delivery. '
            'Publish spectre-superagent mail-reviewed --run <run-id> with check_request_id, ack_request_id '
            'and one reviews entry per message (message_id, action; request_id for reply or cleanup). '
            'Supply JSON on stdin: {"check_request_id":"<check mutation requestId>",'
            '"ack_request_id":"<ack mutation requestId>","reviews":[{"message_id":"<message id>",'
            '"action":"reviewed"}]}. For questions use action answered and reply request_id; '
            'for worker_done use released or retained and cleanup request_id, or reused with '
            'worker-start request_id and new dispatch_id. Never mark worker_done merely reviewed. '
            'Use native receipt request IDs, not terminal-prompt request IDs. '
            'Review does not grant new execution, publication or funding permission.')
