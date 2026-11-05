"""Choose spare Orca carriers so dsh-worker names resolve without a restart.

Orca 1.4.247 resolves an unknown worker name only when exactly one canonical
agent's command override is a single executable whose basename is that name.
The override replaces the carrier's own launch, so a carrier has to be absent
from the live terminals and absent from PATH. Coordinator ownership is untouched.
"""
import re

ALIASES = (
    {'alias': 'dsh-worker', 'route': 'clinepass'},
    {'alias': 'dsh-worker-go', 'route': 'opencode-go'},
)

# stdin-after-start entries in Orca 1.4.247 whose normal program is not part of
# the working set. Order is the preference when several are free.
CANDIDATES = (
    {'agent': 'jcode', 'process': 'jcode'},
    {'agent': 'freebuff', 'process': 'freebuff'},
    {'agent': 'crush', 'process': 'crush'},
    {'agent': 'codebuff', 'process': 'codebuff'},
    {'agent': 'goose', 'process': 'goose'},
    {'agent': 'kilo', 'process': 'kilo'},
    {'agent': 'amp', 'process': 'amp'},
)

PROTECTED = frozenset({
    'claude', 'claude-agent-teams', 'codebuddy', 'openclaude', 'codex',
    'autohand', 'opencode', 'opencode2', 'mimo', 'pi', 'omp', 'qodercli',
    'qoderclicn', 'gemini', 'agy', 'cursor', 'grok', 'dsh', 'zcode', 'devin',
    'muse', 'kimi', 'copilot', 'aider', 'cline', 'kiro', 'hermes',
})

_PROCESS = re.compile(r'[a-z][a-z0-9-]{0,30}\Z')
_UNSAFE = set('\x00\r\n$`|&;<>(){}[]*?! \t\'"\\')


def command_basename(command):
    text = command.strip()
    if not text or any(character in text for character in _UNSAFE):
        return None
    name = text.rsplit('/', 1)[-1]
    for suffix in ('.exe', '.cmd', '.bat'):
        if name.lower().endswith(suffix):
            name = name[:-len(suffix)]
    return name or None


def _occupied(overrides, alias, command):
    holders = [agent for agent, value in overrides.items() if command_basename(value) == alias]
    if not holders:
        return None
    if len(holders) > 1 or overrides.get(holders[0], '').strip() != command:
        raise ValueError(f'{alias} is already bound to a different launcher')
    return holders[0]


def plan(bin_dir, script_dir, live_agents, overrides, disabled, default_agent, installed_names,
        configured_agents=()):
    """Return two carrier bindings. Raise when a safe pair does not exist."""
    if script_dir.name != 'scripts' or not script_dir.is_absolute() or not bin_dir.is_absolute():
        raise ValueError('worker alias paths must be absolute and the scripts directory must be named scripts')
    live = set(live_agents)
    blocked = set(disabled) | set(configured_agents)
    installed = set(installed_names)
    current = {str(agent): str(value) for agent, value in overrides.items()}
    taken = set()
    bindings = []
    for item in ALIASES:
        command = str(bin_dir / item['alias'])
        if any(character in command for character in _UNSAFE):
            raise ValueError('worker launcher path contains a character Orca rejects')
        holder = _occupied(current, item['alias'], command)
        chosen = None
        for candidate in CANDIDATES:
            agent, process = candidate['agent'], candidate['process']
            if (agent in PROTECTED or process in PROTECTED or not _PROCESS.fullmatch(process)
                    or agent in live or agent in blocked or agent == default_agent
                    or agent in installed or process in installed or agent in taken):
                continue
            existing = current.get(agent, '').strip()
            if holder == agent or (holder is None and existing in ('', command)):
                chosen = candidate
                break
        if chosen is None or (holder is not None and holder != chosen['agent']):
            raise ValueError(f'no spare Orca agent is free to carry {item["alias"]}')
        taken.add(chosen['agent'])
        bindings.append({
            'alias': item['alias'],
            'route': item['route'],
            'agent': chosen['agent'],
            'process': chosen['process'],
            'command': command,
            'script': str(script_dir / f'{chosen["process"]}.py'),
        })
    return bindings


def carrier_map(bindings):
    return ''.join(f'{item["route"]} {item["script"]}\n' for item in bindings)
