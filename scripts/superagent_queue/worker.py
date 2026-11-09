"""Keep a visible native worker ready while concrete executions retain their receipts."""
from dataclasses import dataclass, replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

from . import assignment, execution, worker_input, worker_status
from .model import identifier, timestamp
from .settings import read_private

EXECUTOR = Path('/usr/local/bin/spectre-dsh-execution')


@dataclass(frozen=True)
class State:
    child: object = None
    active: str = None
    pending: tuple = ()
    status_agent: str = None


class Worker:
    def __init__(self, root, cli, route, caller, output):
        if route not in execution.ROUTES or any(not caller.get(key) for key in (
                'ORCA_TERMINAL_HANDLE', 'ORCA_PANE_KEY', 'ORCA_WORKTREE_ID')):
            raise ValueError('native worker requires an explicit route and registered Orca pane')
        handle = identifier(caller['ORCA_TERMINAL_HANDLE'])
        self.root, self.cli, self.route, self.caller = Path(root), cli, route, dict(caller)
        self.output = output
        self.spool = execution.private_directory(self.root.parent / 'worker-input' / handle)
        self.state = State()

    @property
    def child(self):
        return self.state.child

    @property
    def active(self):
        return self.state.active

    @property
    def pending(self):
        return self.state.pending

    def report(self, value):
        phases = {'ready': 'ready', 'execution_started': 'working', 'execution_exited': 'ready',
                  'execution_refused': 'blocked', 'waiting_existing_execution': 'working'}
        if value.get('state') in phases:
            name = 'DSH Worker Go' if self.route == 'opencode-go' else 'DSH Worker'
            phase = 'working' if self.child is not None else phases[value['state']]
            self.output.write(f'\x1b]0;{name} {phase}\x07')
        self.output.write(worker_status.frame(self.state.status_agent, value, self.child is not None))
        self.output.write(json.dumps(value, sort_keys=True) + '\n')
        self.output.flush()

    def submit(self, prompt):
        task, dispatch, text = worker_input.assignment(prompt)
        key = hashlib.sha256(prompt.encode()).hexdigest()
        record = {'task_id': task, 'dispatch_id': dispatch, 'prompt': prompt,
                  'task': text, 'received_at': timestamp(None), 'route': self.route}
        record = {**record, 'prompt': assignment.CAPABILITY.sub('', prompt)}
        try:
            execution.persist(self.spool, key + '.json', record)
        except FileExistsError:
            self.report({'state': 'duplicate_input', 'dispatch_id': dispatch})
            return
        source = self.spool / (key + '.preamble.txt')
        descriptor = os.open(source, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as output:
            output.write(prompt)
            output.flush()
            os.fsync(output.fileno())
        self.report({'state': 'input_recorded', 'dispatch_id': dispatch, 'input_id': key})
        if self.child is not None:
            self.state = replace(self.state, pending=(*self.pending, key))
            return
        self.start(key)

    def start(self, key):
        record = read_private(self.spool / (key + '.json'))
        dispatch = record['dispatch_id']
        source = self.spool / (key + '.txt')
        try:
            descriptor = os.open(source, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'w') as output:
                output.write(record['task'])
                output.flush()
                os.fsync(output.fileno())
            _, preamble, native = assignment.prepare(self.cli, record['task_id'], dispatch, source,
                                                       self.route, self.caller)
            received = self.spool / (key + '.preamble.txt')
            assignment.received_preamble(assignment.packet_text(received), preamble, native)
            prefix = [str(EXECUTOR)] if EXECUTOR.is_file() else [
                sys.executable, '-I', '-B', str(Path(__file__).with_name('native_executor.py'))]
            command = [*prefix, '--root', str(self.root), '--orca', self.cli, 'run',
                       '--task', record['task_id'], '--dispatch', dispatch,
                       '--packet', str(source), '--preamble', str(received), '--route', self.route]
            child = subprocess.Popen(command, env=self.caller, stdin=subprocess.DEVNULL, start_new_session=True)
            self.state = replace(self.state, child=child, active=dispatch)
            self.report({'state': 'execution_started', 'dispatch_id': dispatch, 'pid': child.pid})
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
            execution.persist(self.spool, key + '.refused.json', {'dispatch_id': dispatch, 'error': str(exc)})
            self.report({'state': 'execution_refused', 'dispatch_id': dispatch, 'error': str(exc)})

    def settle(self, drain=True):
        if self.child is None or self.child.poll() is None:
            return
        code = self.child.wait()
        dispatch = self.active
        execution.persist(self.spool, dispatch + '.exit.json', {
            'dispatch_id': dispatch, 'returncode': code, 'finished_at': timestamp(None),
            'execution_only': True})
        self.state = replace(self.state, child=None, active=None)
        self.report({'state': 'execution_exited', 'dispatch_id': dispatch, 'returncode': code})
        while drain and self.pending and self.child is None:
            key, *remaining = self.pending
            self.state = replace(self.state, pending=tuple(remaining))
            self.start(key)
