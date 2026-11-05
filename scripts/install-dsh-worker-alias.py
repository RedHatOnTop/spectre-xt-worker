#!/usr/bin/env python3
"""Bind dsh-worker names on the running Orca without restarting it.

The command override replaces one spare agent's normal launch. It refuses an
agent that is open, installed, disabled, or already customized. It does not
call worker-start.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
from orca_runtime.worker_alias import ALIASES, CANDIDATES, carrier_map, command_basename, plan

CLIENT = '/home/person/.local/share/orca/resources/app.asar.unpacked/out/cli/runtime-client.js'
RECOGNIZER = '/home/person/.local/share/orca/resources/app.asar.unpacked/out/shared/agent-process-recognition.js'
SETTINGS_JS = r'''
import {createRequire} from "module";
const require = createRequire(process.env.ORCA_RUNTIME_CLIENT);
const {RuntimeClient} = require(process.env.ORCA_RUNTIME_CLIENT);
const fs = require("fs");
const input = JSON.parse(fs.readFileSync(0, "utf8"));
function project(response) {
  const settings = response && response.result && response.result.settings;
  if (!settings || typeof settings !== "object") throw new Error("agent launch settings missing");
  return settings;
}
const client = new RuntimeClient();
let current = project(await client.call("settings.getAgentLaunch", {}));
const applied = [];
for (const change of input.changes || []) {
  const overrides = current.agentCmdOverrides || {};
  if ((overrides[change.agent] || "") === change.value) {
    applied.push(change.agent);
    continue;
  }
  await client.call("settings.mutateAgentLaunch", {type: "command", agent: change.agent, value: change.value});
  current = project(await client.call("settings.getAgentLaunch", {}));
  applied.push(change.agent);
}
const fresh = project(await client.call("settings.getAgentLaunch", {}));
const overrides = fresh.agentCmdOverrides || {};
process.stdout.write(JSON.stringify({
  defaultTuiAgent: fresh.defaultTuiAgent || null,
  disabledTuiAgents: fresh.disabledTuiAgents || [],
  overrides: Object.fromEntries(applied.map((agent) => [agent, overrides[agent] || ""]))
}));
'''


def orca_json(orca, *args):
    result = subprocess.run([orca, *args], capture_output=True, text=True, check=False, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f'orca exited {result.returncode}')
    return json.loads(result.stdout)


def live_agents(orca):
    payload = orca_json(orca, 'terminal', 'list', '--limit', '200', '--json')
    rows = payload.get('result', payload).get('terminals', [])
    return {row['agentIdentity'] for row in rows if isinstance(row.get('agentIdentity'), str) and row['agentIdentity']}


def read_settings(changes):
    env = {**os.environ, 'ORCA_RUNTIME_CLIENT': CLIENT}
    result = subprocess.run(['node', '--input-type=module', '-e', SETTINGS_JS],
                            input=json.dumps({'changes': changes}), capture_output=True, text=True,
                            check=False, timeout=30, env=env)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f'settings call exited {result.returncode}')
    return json.loads(result.stdout)


def installed_names():
    return {name for candidate in CANDIDATES for name in (candidate['agent'], candidate['process'])
            if shutil.which(name)}


def place_script(source, script_dir, bindings):
    script_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = script_dir / 'spectre-dsh-worker.py'
    temporary = script_dir / '.spectre-dsh-worker.py.new'
    shutil.copy2(source, temporary)
    temporary.chmod(0o755)
    os.replace(temporary, target)
    for item in bindings:
        link = script_dir / f'{item["process"]}.py'
        if link.is_symlink() or link.exists():
            link.unlink()
        link.symlink_to(target.name)


def compile_launcher(source, root):
    binary = root / 'bin/dsh-worker'
    binary.parent.mkdir(parents=True, exist_ok=True)
    temporary = binary.with_name('.dsh-worker.new')
    subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', '-O2',
                    '-DWORKER_ROOT="' + str(root) + '"', str(source / 'scripts/dsh-worker-launch.c'),
                    '-o', str(temporary)], check=True, capture_output=True, text=True)
    temporary.chmod(0o755)
    os.replace(temporary, binary)
    alias = root / 'bin/dsh-worker-go'
    if alias.is_symlink() or alias.exists():
        alias.unlink()
    alias.symlink_to(binary.name)


def public_link(public, target):
    if public.is_symlink() and public.resolve() == target.resolve():
        return
    if public.exists() or public.is_symlink():
        raise ValueError(f'{public.name} already exists and is not this launcher')
    public.parent.mkdir(parents=True, exist_ok=True)
    temporary = public.with_name('.' + public.name + '.new')
    temporary.symlink_to(target)
    os.replace(temporary, public)


def link_library(root, runtime):
    target = runtime / 'lib/spectre-superagent'
    if not target.is_dir():
        raise ValueError('installed harness snapshot has no worker library')
    link = root / 'lib/spectre-superagent'
    link.parent.mkdir(parents=True, exist_ok=True)
    if link.is_symlink() and link.resolve() == target.resolve():
        return
    if link.exists() and not link.is_symlink():
        raise ValueError('worker library link is a real directory')
    temporary = link.with_name('.spectre-superagent-' + str(os.getpid()))
    temporary.symlink_to(target)
    os.replace(temporary, link)


def recognize(bindings):
    script = (
        'const r=require(process.env.ORCA_RECOGNIZER);'
        'const lines=JSON.parse(require("fs").readFileSync(0,"utf8"));'
        'const found=lines.map((line)=>({line, agent:r.recognizeAgentProcessFromCommandLine(line)?.agent||null}));'
        'process.stdout.write(JSON.stringify(found));'
    )
    lines = [f'python3 {item["script"]} --route {item["route"]}' for item in bindings]
    env = {**os.environ, 'ORCA_RECOGNIZER': RECOGNIZER}
    result = subprocess.run(['node', '-e', script], input=json.dumps(lines), capture_output=True,
                            text=True, check=False, timeout=20, env=env)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or 'process recognition failed')
    found = json.loads(result.stdout)
    for item, row in zip(bindings, found):
        if row['agent'] != item['agent']:
            raise RuntimeError(f'{item["script"]} is recognized as {row["agent"]}, not {item["agent"]}')
    return found


def smoke(command):
    env = {key: value for key, value in os.environ.items()
           if key not in ('ORCA_TERMINAL_HANDLE', 'ORCA_PANE_KEY', 'ORCA_WORKTREE_ID')}
    result = subprocess.run([str(command)], capture_output=True, text=True, check=False, timeout=20,
                            env=env, stdin=subprocess.DEVNULL)
    if result.returncode != 2 or 'native worker requires' not in result.stderr:
        raise RuntimeError(f'launcher smoke exited {result.returncode}: {result.stderr.strip()}')


def current_overrides():
    script = r'''
import {createRequire} from "module";
const require = createRequire(process.env.ORCA_RUNTIME_CLIENT);
const {RuntimeClient} = require(process.env.ORCA_RUNTIME_CLIENT);
const client = new RuntimeClient();
const response = await client.call("settings.getAgentLaunch", {});
const layer = response && response.result && response.result.settings;
if (!layer) throw new Error("agent launch settings missing");
process.stdout.write(JSON.stringify({
  defaultTuiAgent: layer.defaultTuiAgent || null,
  disabledTuiAgents: layer.disabledTuiAgents || [],
  overrides: layer.agentCmdOverrides || {},
  configuredAgents: Object.keys(layer.environmentNames || {})
}));
'''
    env = {**os.environ, 'ORCA_RUNTIME_CLIENT': CLIENT}
    result = subprocess.run(['node', '--input-type=module', '-e', script], capture_output=True,
                            text=True, check=False, timeout=30, env=env)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f'settings read exited {result.returncode}')
    return json.loads(result.stdout)


def install(home, source, orca):
    home, source = Path(home).resolve(), Path(source).resolve()
    root = home / '.local/lib/spectre-superagent'
    if root.is_symlink():
        raise ValueError('worker library root cannot be a symlink')
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)
    orca_bin = str(Path(orca))
    if not Path(orca_bin).is_absolute():
        raise ValueError('worker alias install requires the absolute Orca CLI')
    # The library follows the installed harness snapshot, not this source tree.
    snapshot = (home / '.local/bin/harness-workers').resolve().parents[1]
    link_library(root, snapshot)
    before = current_overrides()
    agents = live_agents(orca_bin)
    bindings = plan(home / '.local/bin', root / 'scripts', agents, before['overrides'],
                    before['disabledTuiAgents'], before['defaultTuiAgent'], installed_names(),
                    before.get('configuredAgents', ()))
    place_script(source / 'scripts/spectre-dsh-worker.py', root / 'scripts', bindings)
    compile_launcher(source, root)
    for item in bindings:
        public_link(Path(item['command']), root / 'bin' / item['alias'])
    temporary = root / '.carrier-map.new'
    temporary.write_text(carrier_map(bindings))
    temporary.chmod(0o600)
    os.replace(temporary, root / 'carrier-map')
    recognize(bindings)
    for item in bindings:
        smoke(item['command'])
    busy = [item['agent'] for item in bindings if item['agent'] in live_agents(orca_bin)]
    if busy:
        raise RuntimeError('carrier became live before settings changed: ' + ', '.join(busy))
    confirmed = read_settings([{'agent': item['agent'], 'value': item['command']} for item in bindings])
    for item in bindings:
        if confirmed['overrides'].get(item['agent']) != item['command']:
            raise RuntimeError(f'{item["agent"]} override did not stick')
    return {'native_start': False, 'session_restart': False, 'bindings': bindings,
            'live_agents': sorted(agents)}


def revert(home):
    home = Path(home).resolve()
    root = (home / '.local/lib/spectre-superagent/bin').resolve()
    current = current_overrides()
    changes = []
    for agent, value in current['overrides'].items():
        alias = command_basename(value)
        if alias not in {item['alias'] for item in ALIASES}:
            continue
        path = Path(value.strip())
        if not path.is_symlink() and not path.is_file():
            continue
        if path.resolve().parent != root:
            continue
        changes.append({'agent': agent, 'value': ''})
    if not changes:
        return {'native_start': False, 'reverted': []}
    read_settings(changes)
    after = current_overrides()
    lingering = [agent for change in changes if after['overrides'].get(change['agent'], '').strip()]
    if lingering:
        raise RuntimeError(f'override remained on {", ".join(lingering)}')
    return {'native_start': False, 'reverted': [change['agent'] for change in changes]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--orca', default=shutil.which('orca-ide'))
    parser.add_argument('--revert', action='store_true')
    args = parser.parse_args()
    try:
        if not args.orca or not Path(args.orca).is_absolute():
            raise ValueError('worker alias install requires the absolute Orca CLI')
        report = revert(args.home) if args.revert else install(args.home, args.source, args.orca)
        print(json.dumps(report, indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, subprocess.TimeoutExpired,
            subprocess.CalledProcessError) as error:
        detail = error.stderr if isinstance(error, subprocess.CalledProcessError) else ''
        print(f'dsh-worker alias: {error}{detail}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
