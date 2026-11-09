#!/usr/bin/env python3
"""Launch bounded DSH implementations from the exact existing Orca coordinator."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib/spectre-superagent'))
from superagent_queue import settings, worker_pool

POLICY = Path.home() / '.config/remote-agent/harness-workers.json'
STATE = Path.home() / '.local/state/remote-agent/harness-workers'


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, default=POLICY)
    parser.add_argument('--state', type=Path, default=STATE)
    parser.add_argument('--orca', default=os.environ.get('ORCA_CLI_COMMAND') or shutil.which('orca-ide'))
    actions = parser.add_subparsers(dest='action', required=True)
    actions.add_parser('policy')
    start = actions.add_parser('start')
    start.add_argument('--apply', action='store_true')
    reconciled = actions.add_parser('reconcile')
    reconciled.add_argument('--run', required=True)
    reconciled.add_argument('--launch', required=True)
    reconciled.add_argument('--request', required=True)
    recovery = actions.add_parser('recover')
    recovery.add_argument('--run', required=True)
    recovery.add_argument('--launch', required=True)
    recovery.add_argument('--request', required=True)
    return parser.parse_args()


def main():
    args = arguments()
    try:
        policy = worker_pool.validate_policy(settings.read_private(args.policy))
        if args.action == 'policy':
            print(json.dumps({'policy': policy, 'execution_authority': 'orca-native', 'native_start': False}, indent=2))
            return 0
        if not args.orca or not Path(args.orca).is_absolute():
            raise ValueError('harness worker control requires the exact absolute Orca CLI executable')
        if args.action in ('reconcile', 'recover'):
            operation = worker_pool.reconcile if args.action == 'reconcile' else worker_pool.recover
            report = operation(args.state, args.orca, policy, args.run, args.launch,
                               args.request, os.environ, os.getcwd())
            print(json.dumps(report, indent=2))
            return 0 if report.get('state') in ('ready', 'failed') else 1
        request = json.load(sys.stdin)
        worker_pool.validate_request(request, policy)
        if not args.apply:
            current = worker_pool.owner(args.orca, request, os.environ, os.getcwd())
            report = {'native_start': False, 'request': request, 'coordinator': current, 'policy': policy}
        else:
            report = worker_pool.start(args.state, args.orca, policy, request, os.environ, os.getcwd())
        print(json.dumps(report, indent=2))
        return 1 if report.get('state') in ('failed', 'outcome_unknown', 'reconciliation_required') else 0
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f'harness-workers: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
