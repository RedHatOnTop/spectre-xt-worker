"""Match native receipts, including explicitly configured single-executable aliases."""
import json
import os
import re
from pathlib import Path
import subprocess

from orca_runtime.worker_alias import CANDIDATES, command_basename
from .model import identifier

CARRIERS = frozenset(row['agent'] for row in CANDIDATES)
CLIENT = Path.home() / '.local/share/orca/resources/app.asar.unpacked/out/cli/runtime-client.js'
SETTINGS = '''
const {RuntimeClient} = require(process.env.ORCA_RUNTIME_CLIENT);
new RuntimeClient().call('settings.getAgentLaunch', {}).then(value => {
  const settings = value?.result?.settings;
  if (!settings) throw new Error('native launch settings missing');
  process.stdout.write(JSON.stringify(settings.agentCmdOverrides || {}));
}).catch(error => { process.stderr.write(String(error)); process.exitCode = 1; });
'''


def alias_agent(alias, overrides, public_bin):
    if alias not in ('dsh-worker', 'dsh-worker-go') or not isinstance(overrides, dict):
        raise ValueError('native alias requires an approved DSH name and launch settings')
    reference = re.compile(r'(?:^|/)' + re.escape(alias) + r'(?=$|[\s\x27\x22])')
    if any(isinstance(command, str) and reference.search(command) and command_basename(command) != alias
           for command in overrides.values()):
        raise ValueError('native worker alias must be a single executable without arguments')
    holders = tuple(agent for agent, command in overrides.items()
                    if isinstance(command, str) and command_basename(command) == alias)
    if not holders:
        return alias
    if (len(holders) != 1 or holders[0] not in CARRIERS
            or overrides[holders[0]].strip() != str(public_bin / alias)):
        raise ValueError('native worker alias is not one exact approved spare carrier')
    return holders[0]


def resolve_native(cli, alias):
    home = Path.home()
    known = (home / '.local/bin/orca-ide', home / '.config/orca/linux-orca-cli-shim/orca',
             home / '.local/share/orca/resources/bin/orca-ide')
    if not CLIENT.is_file() or Path(cli).resolve() not in tuple(path.resolve() for path in known):
        return alias
    result = subprocess.run(['node', '-e', SETTINGS], capture_output=True, text=True,
                            check=False, timeout=20, env={**os.environ, 'ORCA_RUNTIME_CLIENT': str(CLIENT)})
    if result.returncode:
        raise RuntimeError(f'native alias settings read exited {result.returncode}: {result.stderr}')
    return alias_agent(alias, json.loads(result.stdout), home / '.local/bin')


def matches(request, value, native_agent):
    if not isinstance(value, dict) or value.get('state') not in ('ready', 'failed'):
        return False
    launch, mutation, effects = value.get('launch'), value.get('mutation'), value.get('effects')
    if not isinstance(mutation, dict) or not isinstance(effects, list):
        return False
    reused = tuple(row for row in effects if isinstance(row, dict) and row.get('kind') == 'terminal'
                   and row.get('role') == 'agent')
    reuse = request.get('terminal')
    prompt = value.get('prompt')
    if reuse and (len(reused) != 1 or reused[0].get('id') != reuse or reused[0].get('action') != 'reused'
                  or not isinstance(prompt, dict) or prompt.get('processIncarnation') != request['terminal_incarnation']):
        return False
    if launch is None and not reuse:
        return False
    if launch is not None:
        if not isinstance(launch, dict):
            return False
        effective = launch.get('effective')
        if effective is None and not reuse:
            return False
        if effective is not None and (not isinstance(effective, dict)
                or effective.get('agent') not in ((None, native_agent) if reuse else (native_agent,))):
            return False
        requested = launch.get('requested')
        if requested is not None and (not isinstance(requested, dict)
                or requested.get('agent') not in ((None, native_agent) if reuse else (native_agent,))):
            return False
    try:
        identifier(value.get('dispatchId'))
        identifier(mutation.get('requestId'))
    except ValueError:
        return False
    workspaces = tuple(row.get('id') for row in effects if isinstance(row, dict) and row.get('kind') == 'worktree')
    return (value.get('runId') == request['run_id'] and value.get('taskId') == request['task_id']
            and (workspaces == (request['worktree_id'],) or (not workspaces and not reuse))
            and any(isinstance(row, dict) and row.get('kind') == 'terminal' and row.get('role') == 'agent'
                    and isinstance(row.get('id'), str) and row['id'] for row in effects))
