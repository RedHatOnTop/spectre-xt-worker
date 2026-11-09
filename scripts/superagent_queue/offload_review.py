"""Validate exact reviewed native settlement before touching an offload resource."""
import hashlib
import json
from pathlib import Path
import re

from orca_runtime import observe
from . import mail_obligation, offload_artifact, offload_receipt
from .model import encoded, identifier
from .target import owner


def reviewed(journal, run_id, message_id):
    row = journal.connection.execute('''SELECT mail_review.body,event.content FROM mail_review
        JOIN event ON event.source='orca-inbox' AND event.event_id=mail_review.message_id
        JOIN effect ON effect.key=event.effect_key AND effect.lane=mail_review.run_id
        WHERE mail_review.run_id=? AND mail_review.message_id=?''', (run_id, message_id)).fetchone()
    if row is None:
        raise ValueError('offload cleanup requires a reviewed native completion')
    body, message = json.loads(row['body']), json.loads(row['content'])
    if message.get('type') != 'worker_done' or body.get('review', {}).get('message_id') != message_id:
        raise ValueError('offload cleanup requires its original reviewed native completion')
    return body, message


def delivery(cli, target, review, message):
    proof = review['proof']
    checked = mail_obligation.request(cli, proof['check_request_id'], 'orchestration.check')
    acked = mail_obligation.request(cli, proof['ack_request_id'], 'orchestration.check')
    matches = [row for row in checked.get('messages', []) if row.get('id') == message['id']]
    if (checked.get('runId') != target['run_id'] or acked.get('runId') != target['run_id']
            or checked.get('deliveryId') != proof['delivery_id']
            or acked.get('acknowledged') != proof['delivery_id']
            or hashlib.sha256(encoded(checked).encode()).hexdigest() != proof['receipt_sha256']
            or len(matches) != 1 or {key: matches[0].get(key) for key in message} != message):
        raise ValueError('offload review no longer matches its original whole native Delivery')
    mail_obligation.completion(cli, message, review['review'], target['run_id'])


def prepare(journal, target, job_id, data, caller, cli):
    identifier(job_id)
    encoded(data)
    if (set(data) != {'message_id', 'artifact_sha256'} or not isinstance(data['artifact_sha256'], str)
            or not re.fullmatch(r'[0-9a-f]{64}', data['artifact_sha256'])):
        raise ValueError('offload cleanup requires exact completion message and artifact digest')
    identifier(data['message_id'])
    current = owner(target, observe.collect(cli, run_ids=(target['run_id'],), include_workers=False))
    if (caller.get('ORCA_TERMINAL_HANDLE') != target['terminal_handle']
            or caller.get('ORCA_PANE_KEY') != current.pane_key):
        raise ValueError('offload review requires the exact current coordinator pane')
    root = journal.path.parent / 'offloads'
    record = offload_receipt.inspect(root, job_id)
    if record['observation'] != 'exited' or record['binding']['run_id'] != target['run_id']:
        raise ValueError('offload cleanup requires its original Run and actual wrapper exit')
    artifact = offload_artifact.observe(root, record)
    if artifact is None or hashlib.sha256(encoded(artifact).encode()).hexdigest() != data['artifact_sha256']:
        raise ValueError('offload review does not identify its exact verified artifact snapshot')
    spec = offload_artifact.packet(record)
    offload_artifact.helper().verify(Path(artifact['artifact_root']),
        record['job_id'], record['command_sha256'], spec['artifacts'])
    review, message = reviewed(journal, target['run_id'], data['message_id'])
    payload = json.loads(message.get('payload') or '{}')
    if (payload.get('taskId') != record['binding']['task_id']
            or payload.get('dispatchId') != record['binding']['dispatch_id']
            or review['review']['action'] not in ('released', 'retained', 'reused')):
        raise ValueError('offload cleanup requires its exact accepted native completion and resource decision')
    result = offload_artifact.remote_result(record)
    if payload.get('outcome') == 'succeeded' and result['returncode'] != 0:
        raise ValueError('native offload outcome conflicts with its actual remote command exit')
    delivery(cli, target, review, message)
    return {'job_id': job_id, 'dispatch_id': record['binding']['dispatch_id'],
            'artifact_sha256': data['artifact_sha256'], 'mail_review': review,
            'native_mutations': False, 'capacity_released': False}
