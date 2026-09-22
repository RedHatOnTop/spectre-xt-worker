"""CLI boundaries for unattended ticks and provider health."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time

from . import providers, runtime
from .io import read_json, write_json
from worker_state.client import StateClient
from worker_state.qoder_jsonl import load_workers, workers_path


class FixtureClient:
    def __init__(self, snapshots):
        self.snapshots = snapshots

    def snapshot(self, worker):
        return self.snapshots.get(worker, {'goal': {'state': 'UNKNOWN'}, 'policy': {}})


def loop_main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--scan', action='store_true')
    parser.add_argument('--snapshot-file', type=Path)
    parser.add_argument('--workers-file', type=Path, default=workers_path())
    args = parser.parse_args(argv)
    if args.snapshot_file and not args.dry_run:
        parser.error('--snapshot-file requires --dry-run')
    if not runtime.enabled(os.environ, 'SPECTRE_LOOP'):
        print(json.dumps({'ok': True, 'disabled': True, 'dry_run': args.dry_run}))
        return 0
    try:
        env = dict(os.environ)
        client = FixtureClient(read_json(args.snapshot_file)) if args.snapshot_file else StateClient(timeout=2)
        workers = load_workers(args.workers_file)
        if not workers and runtime.enabled(env, 'SPECTRE_LOOP'):
            raise ValueError('worker registry is empty or unreadable')
        path = Path(env.get('SPECTRE_LOOP_STATE', Path.home() / '.local/state/remote-agent/spectre-loop.json'))
        output = runtime.tick(workers, client, path, env, dry=args.dry_run)
    except (OSError, ValueError, RuntimeError) as exc:
        output = {'ok': False, 'error': str(exc)}
    print(json.dumps(output, sort_keys=True))
    return 0 if output['ok'] else 1


def provider_main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--modes', type=Path, default=Path.home() / '.codex/modes')
    parser.add_argument('--state', type=Path, default=Path.home() / '.local/state/remote-agent/codex-provider.json')
    parser.add_argument('--plus-rate-limited', action='store_true')
    args = parser.parse_args(argv)
    try:
        now = time.time()
        previous = read_json(args.state)
        if args.plus_rate_limited:
            output = {**previous, 'ok': False, 'id': 'openai', 'checked_at': now,
                      'cooldown_until': now + providers.PLUS_COOLDOWN}
        else:
            output = providers.choose(read_json(args.modes / 'providers.json'), previous, now,
                                      lambda row: providers.probe(row, args.modes))
        write_json(args.state, output)
    except (OSError, ValueError) as exc:
        output = {'ok': False, 'error': type(exc).__name__}
    print(json.dumps(output, sort_keys=True))
    return 0 if output['ok'] else 1
