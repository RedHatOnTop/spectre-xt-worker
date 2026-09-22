#!/usr/bin/env python3
"""Synchronize native worker pins by argv/model; default dry-run."""
from pathlib import Path
import argparse
import json
import sys

for parent in (Path(__file__).resolve().parent, Path(__file__).resolve().parent.parent / 'lib/spectre-worker-state',
               Path('/usr/local/lib/spectre-worker-state')):
    if (parent / 'control_plane').is_dir():
        sys.path.insert(0, str(parent))
        break

from control_plane import inventory, pins, runtime
from control_plane.io import locked, read_json, run, write_json
from worker_state.qoder_jsonl import workers_path


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--workers-file', type=Path, default=workers_path())
    parser.add_argument('--proc-root', type=Path, default=Path('/proc'))
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--flash-terminal')
    args = parser.parse_args(argv)
    try:
        listed = run(['orca-ide', 'terminal', 'list', '--json'])
        if not listed['ok']:
            raise RuntimeError('Orca terminal list failed')
        terminals = listed['parsed']['result']['terminals']
        with locked(args.workers_file.with_suffix('.lock')):
            payload = read_json(args.workers_file)
            runtime.validate_workers(payload['workers'])
            processes = inventory.scan(args.proc_root)
            workers, changes = pins.sync(payload['workers'], terminals, processes)
            if args.flash_terminal:
                workers = pins.bind_flash(workers, terminals, processes, args.flash_terminal)
                changes = [*changes, {'worker': 'minecraft', 'role': 'flash', 'ok': True, 'handle': args.flash_terminal}]
            if args.apply:
                write_json(args.workers_file, {**payload, 'workers': workers})
        output = {'ok': all(row['ok'] for row in changes), 'dry_run': not args.apply, 'changes': changes}
    except (OSError, ValueError, KeyError, RuntimeError) as exc:
        output = {'ok': False, 'error': str(exc)}
    print(json.dumps(output, sort_keys=True))
    return 0 if output['ok'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
