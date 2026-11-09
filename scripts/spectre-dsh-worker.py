#!/usr/bin/env python3
"""Serve framed native assignments in one registered visible Orca worker pane."""
import argparse
import codecs
from dataclasses import replace
import fcntl
import os
from pathlib import Path
import selectors
import signal
import sys
import termios
import tty

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'lib/spectre-superagent'))
from superagent_queue import execution, worker, worker_input, worker_status

ROOT = Path.home() / '.local/state/remote-agent/superagent/executions'


def serve(controller):
    descriptor = sys.stdin.fileno()
    original = termios.tcgetattr(descriptor) if sys.stdin.isatty() else None
    decoder = codecs.getincrementaldecoder('utf-8')('strict')
    state = worker_input.initial()
    stopping = False

    def stop(signum, frame):
        nonlocal stopping
        stopping = True
        controller.report({'state': 'shutdown_requested', 'active_dispatch_id': controller.active})

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, stop)
    selector = selectors.DefaultSelector()
    selector.register(descriptor, selectors.EVENT_READ)
    try:
        if original is not None:
            tty.setraw(descriptor)
            sys.stdout.write('\x1b[?2004h')
            sys.stdout.flush()
        controller.report({'state': 'ready', 'route': controller.route, 'protocol': 'native-bracketed-paste-v1'})
        while not stopping:
            controller.settle(drain=not stopping)
            readable = selector.select(0.25)
            if stopping or not readable:
                continue
            data = os.read(descriptor, 4096)
            if not data:
                decoder.decode(b'', final=True)
                worker_input.finish(state)
                stopping = True
                selector.unregister(descriptor)
                continue
            state, prompts = worker_input.feed(state, decoder.decode(data))
            for prompt in prompts:
                controller.submit(prompt)
    finally:
        selector.close()
        if controller.child is not None:
            controller.report({'state': 'waiting_existing_execution', 'dispatch_id': controller.active})
            controller.child.wait()
            controller.settle(drain=False)
        if original is not None:
            termios.tcsetattr(descriptor, termios.TCSADRAIN, original)
            sys.stdout.write('\x1b[?2004l')
            sys.stdout.flush()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--route', choices=sorted(execution.ROUTES), required=True)
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--orca', default=str(Path.home() / '.local/bin/orca-ide'))
    args = parser.parse_args()
    try:
        controller = worker.Worker(args.root, args.orca, args.route, os.environ, sys.stdout)
        lock = os.open(controller.spool / 'process.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            controller.state = replace(controller.state, status_agent=worker_status.bind(args.orca, os.environ))
            serve(controller)
        finally:
            os.close(lock)
        return 0
    except (OSError, ValueError, RuntimeError, UnicodeError) as exc:
        print(f'spectre-dsh-worker: {exc}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
