"""User-scope native executor for the existing Go account, without provider fallback."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orca_runtime import observe
from superagent_queue import assignment, execution, go_native

ROOT = Path.home() / '.local/state/remote-agent/superagent/executions'
GO_BINARY = Path.home() / '.opencode/bin/opencode'


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--orca', required=True)
    commands = parser.add_subparsers(dest='action', required=True)
    run = commands.add_parser('run')
    run.add_argument('--task', required=True)
    run.add_argument('--dispatch', required=True)
    run.add_argument('--packet', type=Path, required=True)
    run.add_argument('--preamble', type=Path, required=True)
    run.add_argument('--route', choices=sorted(execution.ROUTES), required=True)
    return parser.parse_args()


def failure_capability(preamble, dispatch):
    if (not isinstance(dispatch, dict) or dispatch.get('contract_version') != 1
            or 'capability_hash' not in dispatch or dispatch.get('capability_revoked_at')):
        raise ValueError('native failure report requires current verified Dispatch authority')
    capabilities = frozenset(assignment.CAPABILITY.findall(preamble))
    expected = dispatch['capability_hash']
    if expected is None and not capabilities:
        return ()
    if len(capabilities) != 1:
        raise ValueError('native failure report requires one exact current Dispatch capability')
    capability = next(iter(capabilities))
    if hashlib.sha256(capability.encode()).hexdigest() != expected:
        raise ValueError('native failure report capability does not match its current Dispatch')
    return ('--dispatch-capability', capability)


def fail_if_unsettled(args, binding, preamble, message):
    value = observe.query(args.orca, ['orchestration', 'worker-show', '--dispatch', args.dispatch, '--full'])
    if value.get('dispatch', {}).get('status') != 'dispatched':
        return False
    dispatch = assignment.confirm(args.orca, binding, args.packet, os.environ)
    authorization = failure_capability(preamble, dispatch)
    try:
        observe.query(args.orca, ['orchestration', 'send', '--from', binding['terminal_handle'],
            '--type', 'worker_done', '--task-id', args.task, '--dispatch-id', args.dispatch,
            *authorization, '--outcome', 'failed',
            '--subject', 'Native Go execution failed', '--body', message])
    except RuntimeError as exc:
        message = str(exc)
        for capability in assignment.CAPABILITY.findall(preamble):
            message = message.replace(capability, '<redacted>')
        raise RuntimeError(message) from None
    return True


def execute(args):
    binding, current, dispatch = assignment.prepare(args.orca, args.task, args.dispatch,
                                                    args.packet, args.route, os.environ)
    preamble = assignment.received_preamble(assignment.packet_text(args.preamble), current, dispatch)
    if args.route != 'opencode-go':
        fail_if_unsettled(args, binding, preamble, 'ClinePass execution is not installed on this host; no alternate provider was used.')
        raise ValueError('ClinePass executor is not installed on this host')
    result = go_native.execute(args.root, binding, dispatch, args.preamble, args.orca,
                              GO_BINARY, source=args.packet)
    message = result.get('error') or f'Go CLI exited {result["returncode"]} without an accepted native worker_done.'
    missing = fail_if_unsettled(args, binding, preamble, message)
    print(json.dumps({key: result.get(key) for key in ('returncode', 'spawned', 'session_id', 'error')}, sort_keys=True))
    return 2 if missing or result.get('error') else result['returncode'] if result['returncode'] >= 0 else 128 - result['returncode']


def main():
    args = arguments()
    try:
        return execute(args)
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        print(f'native-worker-executor: {exc}', file=sys.stderr)
        try:
            value = observe.query(args.orca, ['orchestration', 'worker-show', '--dispatch', args.dispatch, '--full'])
            if value.get('dispatch', {}).get('status') != 'dispatched':
                return 2
            binding, current, dispatch = assignment.prepare(args.orca, args.task, args.dispatch,
                                                            args.packet, args.route, os.environ)
            preamble = assignment.received_preamble(assignment.packet_text(args.preamble), current, dispatch)
            fail_if_unsettled(args, binding, preamble, str(exc))
        except (OSError, ValueError, KeyError, RuntimeError) as report_error:
            print(f'native failure reporting failed: {report_error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
