"""Versioned continuation decisions are operational facts, not task outcomes."""
from .model import encoded, identifier, timestamp
from .target import owner

STATES = frozenset(('working', 'waiting_job', 'waiting_provider', 'waiting_approval',
                    'ready_for_review', 'finished'))
STATE_FIELDS = {'waiting_job': 'job_ids', 'waiting_approval': 'gate_ids',
                'waiting_provider': 'retry_at'}


def validate(data):
    encoded(data)
    for run_id, value in data.items():
        identifier(run_id)
        if not isinstance(value, dict) or value.get('state') not in STATES:
            raise ValueError('continuation disposition requires a structured state')
        field = STATE_FIELDS.get(value['state'])
        allowed = {'state', 'reason'} | ({field} if field else set())
        if set(value) - allowed:
            raise ValueError('continuation disposition has unknown or irrelevant fields')
        if 'reason' in value and (not isinstance(value['reason'], str) or not value['reason'].strip()):
            raise ValueError('continuation disposition reason must be nonempty text')
        if field == 'retry_at':
            if field not in value:
                raise ValueError('waiting_provider requires retry_at')
            timestamp(value[field])
        elif field:
            refs = value.get(field)
            if not isinstance(refs, list) or not refs:
                raise ValueError(f'{value["state"]} requires unique exact blocking references')
            for reference in refs:
                identifier(reference)
            if len(set(refs)) != len(refs):
                raise ValueError(f'{value["state"]} requires unique exact blocking references')
    return data


def effective(journal, baseline):
    validate(baseline)
    return {**baseline, **{run: row['body'] for run, row in journal.current_dispositions().items()}}


def publish(journal, target, value, expected_revision, caller, inventory, now=None):
    validate({target['run_id']: value})
    current = owner(target, inventory)
    if (caller.get('ORCA_TERMINAL_HANDLE') != target['terminal_handle']
            or caller.get('ORCA_PANE_KEY') != current.pane_key):
        raise ValueError('disposition publication requires the exact current native owner pane')
    receipt = {'terminal_handle': target['terminal_handle'], 'pane_key': current.pane_key,
               'worktree_id': target['worktree_id'], 'incarnation_id': target['incarnation_id']}
    return journal.publish_disposition(target['run_id'], value, expected_revision, receipt, now=now)
