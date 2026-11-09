"""Resolve one exact native owner from an immutable runtime snapshot."""
from dataclasses import dataclass

from .model import encoded, identifier

TARGET_FIELDS = frozenset(('run_id', 'terminal_handle', 'worktree_id', 'incarnation_id', 'text'))
IDLE_STATES = frozenset(('done', 'idle'))


@dataclass(frozen=True)
class Owner:
    pane_key: str
    state: str | None
    state_started_at: int | float | None
    prompt: str | None
    last_assistant_message: str | None
    interrupted: bool | None

    @property
    def idle(self):
        return self.state in IDLE_STATES


def validate_payload(payload):
    encoded(payload)
    if set(payload) != TARGET_FIELDS:
        raise ValueError('wake requires exact native target fields')
    for key in ('run_id', 'terminal_handle', 'incarnation_id'):
        identifier(payload[key])
    workspace, text = payload['worktree_id'], payload['text']
    if not isinstance(workspace, str) or not workspace or '\x00' in workspace or len(workspace) > 4096:
        raise ValueError('wake requires a bounded native workspace identity')
    if not isinstance(text, str) or not text.strip() or '\x00' in text:
        raise ValueError('wake requires nonempty text without null bytes')
    return payload


def owner(payload, data):
    runtime = data.get('runtime', {}).get('runtime', {})
    if runtime.get('reachable') is not True or runtime.get('state') != 'ready':
        raise RuntimeError('native runtime is not ready')
    runs = [r for r in data.get('runs', []) if r.get('id') == payload['run_id']
            and not r.get('legacy') and r.get('coordinator_handle') == payload['terminal_handle']]
    terminals = [t for t in data.get('terminals', []) if t.get('handle') == payload['terminal_handle']
                 and t.get('worktreeId') == payload['worktree_id'] and t.get('connected') and t.get('writable')]
    workspaces = [w for w in data.get('worktrees', []) if w.get('worktreeId') == payload['worktree_id']]
    if len(runs) != 1 or len(terminals) != 1 or len(workspaces) != 1:
        raise ValueError('native coordinator ownership cannot be confirmed')
    terminal = terminals[0]
    if terminal.get('incarnationId') != payload['incarnation_id']:
        raise ValueError('native coordinator incarnation changed; reconciliation required')
    pane = terminal.get('paneKey')
    if not pane and terminal.get('tabId') and terminal.get('leafId'):
        pane = f'{terminal["tabId"]}:{terminal["leafId"]}'
    agents = [a for a in workspaces[0].get('agents', []) if pane and a.get('paneKey') == pane]
    agent = agents[0] if len(agents) == 1 else {}
    return Owner(pane or '', agent.get('state'), agent.get('stateStartedAt'), agent.get('prompt'),
                 agent.get('lastAssistantMessage'), agent.get('interrupted'))


def prompt_receipt(payload):
    result = payload.get('result')
    send = result.get('send') if isinstance(result, dict) else None
    prompt = send.get('prompt') if isinstance(send, dict) else None
    return prompt if isinstance(prompt, dict) else {}
