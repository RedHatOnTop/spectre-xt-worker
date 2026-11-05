"""Read native assignment authority from the exact execution pane."""
import hashlib
import os
from pathlib import Path
import stat
import re

from orca_runtime import observe
from . import execution
from .model import identifier, MAX_JSON_BYTES

TASK_MARKER = '\n=== TASK ===\n'
CAPABILITY = re.compile(r' --dispatch-capability ([A-Za-z0-9_-]+)(?=\s|$)')


def packet_text(path):
    path = Path(path).absolute()
    if path != path.resolve():
        raise ValueError('assignment packet cannot be a symlink')
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        entry = os.fstat(descriptor)
        if (not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid()
                or stat.S_IMODE(entry.st_mode) != 0o600 or entry.st_size > MAX_JSON_BYTES):
            raise ValueError('assignment packet must be a bounded private regular file')
        with os.fdopen(descriptor, encoding='utf-8', closefd=False) as source:
            text = source.read(MAX_JSON_BYTES + 1)
    finally:
        os.close(descriptor)
    if not text.strip() or '\x00' in text or len(text.encode()) > MAX_JSON_BYTES:
        raise ValueError('assignment packet must contain bounded nonempty text')
    return text


def current_dispatch(cli, task_id, dispatch_id, caller):
    handle, pane = caller.get('ORCA_TERMINAL_HANDLE'), caller.get('ORCA_PANE_KEY')
    if not handle or not pane or not caller.get('ORCA_WORKTREE_ID'):
        raise ValueError('DSH execution requires its exact registered Orca pane')
    identifier(task_id)
    identifier(dispatch_id)
    result = observe.query(cli, ['orchestration', 'dispatch-show', '--task', task_id])
    dispatch = result.get('dispatch')
    if (not isinstance(dispatch, dict) or dispatch.get('id') != dispatch_id
            or dispatch.get('task_id') != task_id or dispatch.get('status') != 'dispatched'
            or dispatch.get('contract_version') != 1 or dispatch.get('capability_revoked_at')
            or dispatch.get('assignee_handle') != handle or dispatch.get('assignee_pane_key') != pane):
        raise ValueError('current native Dispatch does not authorize this exact pane')
    run = observe.show_run(cli, identifier(dispatch['run_id']))
    if run.get('legacy'):
        raise ValueError('native execution Run cannot be confirmed')
    coordinator = identifier(run.get('coordinator_handle'))
    source = observe.query(cli, ['orchestration', 'dispatch-show', '--task', task_id,
                                 '--preamble', '--from', coordinator])
    if source.get('dispatch') != dispatch:
        raise ValueError('native Dispatch changed while reading its worker preamble')
    preamble = source.get('preamble')
    if not isinstance(preamble, str) or TASK_MARKER not in preamble or len(preamble.encode()) > MAX_JSON_BYTES:
        raise ValueError('native Dispatch lacks its bounded exact worker preamble')
    return dispatch, preamble


def current_terminal(cli, caller, cwd):
    handle = caller['ORCA_TERMINAL_HANDLE']
    terminal = observe.query(cli, ['terminal', 'show', '--terminal', handle]).get('terminal')
    workspaces = observe.collect_workspaces(cli)
    reported = terminal.get('worktreePath') if isinstance(terminal, dict) else None
    # Folder terminals report an empty worktreePath. Their path is the registered
    # workspace row, checked below. A reported path still has to equal cwd.
    if (not isinstance(terminal, dict) or terminal.get('handle') != handle
            or not terminal.get('connected') or not terminal.get('writable')
            or terminal.get('worktreeId') != caller['ORCA_WORKTREE_ID']
            or not isinstance(reported, str) or (reported and reported != cwd)
            or not terminal.get('incarnationId')):
        raise ValueError('native terminal does not match the execution workspace')
    pane = terminal.get('paneKey') or f'{terminal.get("tabId", "")}:{terminal.get("leafId", "")}'
    if pane != caller['ORCA_PANE_KEY'] or not any(
            row.get('worktreeId') == terminal['worktreeId'] and row.get('path') == cwd for row in workspaces):
        raise ValueError('native execution pane or registered workspace cannot be confirmed')
    return terminal


def prepare(cli, task_id, dispatch_id, packet, route, caller, cwd=None, routes=execution.ROUTES):
    dispatch, preamble = current_dispatch(cli, task_id, dispatch_id, caller)
    text = packet_text(packet)
    if preamble.split(TASK_MARKER, 1)[1].strip() != text.strip():
        raise ValueError('assignment packet does not match the native Task')
    directory = os.getcwd() if cwd is None else cwd
    terminal = current_terminal(cli, caller, directory)
    incarnation = f'{terminal.get("ptyId", "")}:{terminal["incarnationId"]}'
    if dispatch.get('process_incarnation') != incarnation:
        raise ValueError('native Dispatch process incarnation no longer matches its pane')
    binding = {'run_id': dispatch['run_id'], 'task_id': task_id, 'dispatch_id': dispatch_id,
               'terminal_handle': terminal['handle'], 'pane_key': caller['ORCA_PANE_KEY'],
               'worktree_id': terminal['worktreeId'], 'workspace_path': directory,
               'incarnation_id': terminal['incarnationId'], 'route': route,
               'packet_sha256': hashlib.sha256(text.encode()).hexdigest()}
    return execution.validate(binding, routes=routes), preamble, dispatch


def confirm(cli, binding, packet, caller, cwd=None):
    current, _, dispatch = prepare(cli, binding['task_id'], binding['dispatch_id'], packet,
                                   binding['route'], caller, cwd=cwd)
    if current != binding:
        raise ValueError('native execution binding changed before model input')
    return dispatch


def received_preamble(received, preview, dispatch):
    values = CAPABILITY.findall(received)
    stripped = CAPABILITY.sub('', received)
    expected = dispatch.get('capability_hash')
    if stripped != preview:
        raise ValueError('received packet differs from the current native preamble')
    if expected:
        if not values or len(set(values)) != 1 or hashlib.sha256(values[0].encode()).hexdigest() != expected:
            raise ValueError('received native capability does not match the current Dispatch')
    elif values:
        raise ValueError('received capability is not authorized by the current Dispatch')
    return received
