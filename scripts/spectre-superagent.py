#!/usr/bin/env python3
"""Durable continuation of approved native Orca work, without duplicate owners."""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib/spectre-superagent'))
from orca_runtime import observe
from superagent_queue import disposition, driver, mail_obligation, offload_artifact, offload_cleanup, offload_recovery, selection, settings
from superagent_queue.store import Journal

CONFIG_DIR = Path.home() / '.config/remote-agent'
STATE_DIR = Path.home() / '.local/state/remote-agent/superagent'


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=CONFIG_DIR / 'superagent.json')
    parser.add_argument('--dispositions', type=Path, default=CONFIG_DIR / 'superagent-dispositions.json')
    parser.add_argument('--journal', type=Path, default=STATE_DIR / 'journal.sqlite3')
    parser.add_argument('--orca', default=str(Path.home() / '.local/bin/orca-ide'))
    actions = parser.add_subparsers(dest='action', required=True)
    actions.add_parser('status')
    tick = actions.add_parser('tick')
    tick.add_argument('--apply', action='store_true')
    enqueue = actions.add_parser('enqueue')
    enqueue.add_argument('--run', required=True)
    publish = actions.add_parser('disposition')
    publish.add_argument('--run', required=True)
    publish.add_argument('--expect-revision', type=int, required=True)
    reviewed = actions.add_parser('mail-reviewed')
    reviewed.add_argument('--run', required=True)
    reconciled = actions.add_parser('offload-reconcile')
    reconciled.add_argument('--run', required=True)
    reconciled.add_argument('--job', required=True)
    reconciled.add_argument('--fetch', action='store_true')
    artifacts = actions.add_parser('offload-artifacts')
    artifacts.add_argument('--run', required=True)
    artifacts.add_argument('--job', required=True)
    artifacts.add_argument('--fetch', action='store_true')
    cleanup = actions.add_parser('offload-cleanup')
    cleanup.add_argument('--run', required=True)
    cleanup.add_argument('--job', required=True)
    cleanup.add_argument('--reconcile', action='store_true')
    return parser.parse_args()


def run(args, journal, config, dispositions):
    dispositions = disposition.effective(journal, dispositions)
    if args.action == 'status':
        return {'authority': 'orca-native', 'queue': journal.summary(), 'dispositions': dispositions,
                'published_dispositions': journal.current_dispositions(),
                'effects': journal.effects(), 'mail_obligations': {
                    row['run_id']: journal.open_mail(row['run_id']) for row in config['coordinators']},
                'selection': selection.status_view(journal),
                'automatic_worker_creation': False}
    if args.action in ('enqueue', 'disposition', 'mail-reviewed', 'offload-reconcile', 'offload-artifacts', 'offload-cleanup'):
        targets = [r for r in config['coordinators'] if r['run_id'] == args.run]
        if len(targets) != 1:
            raise ValueError(f'{args.action} requires one approved native Run')
        if args.action == 'mail-reviewed':
            return mail_obligation.publish(journal, targets[0], json.load(sys.stdin), os.environ,
                observe.collect(args.orca, run_ids=(args.run,), include_workers=False), args.orca)
        if args.action == 'offload-cleanup':
            studios = [row['studio'] for row in config.get('offload', []) if row['run_id'] == args.run]
            if len(studios) != 1:
                raise ValueError('offload cleanup requires one approved existing Studio')
            return offload_cleanup.publish(journal, targets[0], args.job, json.load(sys.stdin), os.environ,
                                           args.orca, studios[0], reconcile=args.reconcile)
        if args.action in ('offload-reconcile', 'offload-artifacts'):
            studios = [row['studio'] for row in config.get('offload', []) if row['run_id'] == args.run]
            if args.fetch and len(studios) != 1:
                raise ValueError('offload fetch requires one approved existing Studio')
            publish = offload_recovery.publish if args.action == 'offload-reconcile' else offload_artifact.publish
            return publish(journal, targets[0], args.job, os.environ, args.orca,
                           studio=studios[0] if args.fetch else None)
        if args.action == 'disposition':
            result = disposition.publish(journal, targets[0], json.load(sys.stdin), args.expect_revision,
                os.environ, observe.collect(args.orca, run_ids=(args.run,), include_workers=False))
            return {'published': result, 'native_mutations': False}
        added = driver.enqueue_context(journal, targets[0], json.load(sys.stdin))
        return {'queued': added, 'queue': journal.summary(), 'delivered': False}
    if not args.apply:
        report = driver.discover(journal, {'coordinators': []}, observe.collect(args.orca), dispositions)
        return {'applied': False, **report, 'queue': journal.summary(), 'configured_runs': config['coordinators']}
    return {'applied': True, **driver.tick(journal, config, dispositions, args.orca)}


def main():
    args = arguments()
    try:
        config = settings.config(args.config)
        dispositions = settings.dispositions(args.dispositions)
        tick_lock = settings.lock(args.journal.with_suffix('.lock')) if args.action == 'tick' else nullcontext()
        with tick_lock, Journal(args.journal) as journal:
            report = run(args, journal, config, dispositions)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report.get('issues') or report.get('queue', {}).get('effects', {}).get('attention') else 0
    except BlockingIOError:
        print('spectre-superagent: another continuity tick owns the operational lock', file=sys.stderr)
        return 75
    except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        print(f'spectre-superagent: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
