#!/usr/bin/env python3
"""Execute an exact native Dispatch visibly and preserve its OS receipt."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib/spectre-superagent'))
from superagent_queue import assignment, execution, free_tier, session_execution, session_profile

ROOT = Path.home() / '.local/state/remote-agent/superagent/executions'
WRAPPER = Path('/usr/local/bin/dsh-superagent-worker')
GO_HOME = Path.home() / '.local/share/fullmoon-dsh-go-verified-20261102'
CLINE_HOME = Path.home() / '.local/share/fullmoon-dsh'
CLINE_KEY = Path.home() / '.config/fullmoon-agent-control/cline_api_key'
FREE_HOME = Path.home() / '.local/share/fullmoon-dsh-free'
FREE_KEY = Path.home() / '.config/omni-proxy/client_key'
GO_KEY = Path.home() / '.config/omni-proxy/opencode_go.key'
DSH_BIN = Path.home() / '.local/share/deepseek-harness-venv/bin/dsh'
LIB = Path(__file__).resolve().parent.parent / 'lib/spectre-superagent'
SOURCE = LIB / 'dsh-worker-session/headless-0.1.5-rc.1.js'
HELPER = LIB / 'dsh-worker-session.mjs'


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--orca', default=str(Path.home() / '.local/bin/orca-ide'))
    actions = parser.add_subparsers(dest='action', required=True)
    run = actions.add_parser('run')
    run.add_argument('--task', required=True)
    run.add_argument('--dispatch', required=True)
    run.add_argument('--packet', type=Path, required=True)
    run.add_argument('--preamble', type=Path)
    run.add_argument('--route', choices=sorted(execution.ROUTES), required=True)
    status = actions.add_parser('inspect')
    status.add_argument('--dispatch', required=True)
    authorize = actions.add_parser('authorize')
    authorize.add_argument('--intent', type=Path, required=True)
    return parser.parse_args()


def execute(args):
    binding, prompt, dispatch = assignment.prepare(args.orca, args.task, args.dispatch, args.packet,
                                                    args.route, os.environ)
    if getattr(args, 'preamble', None) is not None:
        prompt = assignment.received_preamble(assignment.packet_text(args.preamble), prompt, dispatch)
    elif dispatch.get('capability_hash'):
        raise ValueError('supervised native execution requires its original capability preamble')
    text = assignment.packet_text(args.packet)
    decision = free_tier.decide(free_tier.kind_of(text), free_tier.readiness(FREE_HOME, FREE_KEY))
    tier = 'go' if args.route == 'opencode-go' else decision['tier']
    session = session_execution.prepare(args.root, binding, dispatch)
    session_profile.verify(GO_HOME if args.route == 'opencode-go'
                           else (FREE_HOME if tier == 'free' else CLINE_HOME), SOURCE, HELPER)
    packets = execution.private_directory(args.root.parent / 'packets')
    path = packets / f'{args.dispatch}.txt'
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as packet:
        packet.write(prompt)
        packet.flush()
        os.fsync(packet.fileno())
    source = packets / f'{args.dispatch}-source.txt'
    descriptor = os.open(source, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'w') as packet:
        packet.write(text)
        packet.flush()
        os.fsync(packet.fileno())
    env = {**os.environ, 'SPECTRE_PACKET_DIR': str(packets), 'SPECTRE_DSH_BIN': str(DSH_BIN),
           'SPECTRE_DSH_KIND': decision['kind'] or 'unknown', 'SPECTRE_DSH_TIER': tier,
           'SPECTRE_DSH_TIER_REASON': decision['reason'],
           'SPECTRE_DSH_PROFILE': session_profile.PROFILE, 'SPECTRE_DSH_HOME': str(CLINE_HOME),
           'SPECTRE_DSH_KEY': str(CLINE_KEY), 'SPECTRE_DSH_SESSION_ID': session['session_id'],
           'SPECTRE_DSH_SESSION_RESUME': '1' if session['resume'] else '0',
           'SPECTRE_DSH_SESSION_RECEIPT': str(args.root.absolute() / args.dispatch / 'session.json'),
           'SPECTRE_DSH_AUTHORITY_INTENT': str(args.root.absolute() / args.dispatch / 'intent.json')}
    if tier == 'go':
        env = {**env, 'SPECTRE_DSH_GO_HOME': str(GO_HOME), 'SPECTRE_DSH_GO_KEY': str(GO_KEY)}
    elif tier == 'free':
        env = {**env, 'SPECTRE_DSH_FREE_HOME': str(FREE_HOME), 'SPECTRE_DSH_FREE_KEY': str(FREE_KEY),
               'SPECTRE_DSH_HOME': str(FREE_HOME), 'SPECTRE_DSH_KEY': str(FREE_KEY)}
    free_tier.record({'task_id': args.task, 'dispatch_id': args.dispatch, 'route': args.route,
                      'kind': decision['kind'], 'tier': tier, 'reason': decision['reason']})
    command = [str(WRAPPER), '--tier', tier, '--file', str(path)]
    return execution.run(args.root, binding, command, env=env, session=session)


def authorize(args):
    from superagent_queue.settings import read_private
    intent = read_private(args.intent)
    binding = execution.validate(intent['binding'])
    record = execution.inspect(args.intent.parent.parent, binding['dispatch_id'])
    if args.intent.name != 'intent.json' or args.intent.parent.name != binding['dispatch_id']:
        raise ValueError('native authorization requires its exact original intent path')
    if record['observation'] != 'live':
        raise ValueError('native authorization requires its recorded live wrapper')
    source = args.intent.parent.parent.parent / 'packets' / f'{binding["dispatch_id"]}-source.txt'
    assignment.confirm(args.orca, binding, source, os.environ)
    dispatch, preview = assignment.current_dispatch(args.orca, binding['task_id'], binding['dispatch_id'], os.environ)
    preamble = source.with_name(f'{binding["dispatch_id"]}.txt')
    assignment.received_preamble(assignment.packet_text(preamble), preview, dispatch)
    session = intent['session']['session_id']
    if session != os.environ.get('SPECTRE_DSH_SESSION_ID'):
        raise ValueError('native authorization session does not match its intent')
    return {'authorized': True, 'session_id': session, 'dispatch_id': binding['dispatch_id']}


def main():
    args = arguments()
    try:
        if args.action == 'authorize':
            result = authorize(args)
        else:
            result = execution.inspect(args.root, args.dispatch) if args.action == 'inspect' else execute(args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        code = result.get('returncode', 0)
        return code if code >= 0 else 128 - code
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f'spectre-dsh-execution: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
