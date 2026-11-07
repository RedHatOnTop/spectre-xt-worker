"""Publish the reader's real lifecycle; idle is not a Task or model outcome."""
import json
from pathlib import Path

from orca_runtime.worker_alias import CANDIDATES
from . import assignment

AGENTS = frozenset(('dsh-worker', 'dsh-worker-go', *(row['agent'] for row in CANDIDATES)))
PHASES = {'ready': 'done', 'execution_started': 'working', 'execution_exited': 'done',
          'execution_refused': 'blocked', 'waiting_existing_execution': 'working'}


def bind(cli, caller):
    terminal = assignment.current_terminal(cli, caller, str(Path.cwd()))
    agent = terminal.get('agentIdentity')
    if agent not in AGENTS:
        raise ValueError('native worker status requires its exact registered DSH or spare carrier identity')
    return agent


def frame(agent, value, child_active):
    if agent is None or value.get('state') not in PHASES:
        return ''
    if agent not in AGENTS:
        raise ValueError('native worker status cannot impersonate another agent')
    state = 'working' if child_active else PHASES[value['state']]
    payload = {'state': state, 'agentType': agent, 'prompt': ''}
    return '\x1b]9999;' + json.dumps(payload, sort_keys=True) + '\x07'
