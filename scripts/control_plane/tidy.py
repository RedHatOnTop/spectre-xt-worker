"""Select and close unused Orca packet tabs. Dry-run is selection only."""
from __future__ import annotations

from pathlib import Path
import re

from .io import run as run_command

JOB_TITLE = re.compile(r'^(flash|mimo) ([A-Za-z0-9][A-Za-z0-9_-]{0,95})$')
SHELL_TITLES = frozenset({'flash-packets', 'mimo-packets'})
GRACE_SEC = 300


def parse_job_title(title: str) -> tuple[str, str] | None:
    match = JOB_TITLE.fullmatch(str(title or '').strip())
    if not match:
        return None
    return match.group(1), match.group(2)


def is_shell_title(title: str) -> bool:
    return str(title or '').strip() in SHELL_TITLES


def select(terminals: list[dict], pinned_handles: set[str], packet_dir: Path,
           now: float, grace: float = GRACE_SEC) -> list[dict]:
    """Finished job tabs and orphan packet shells.

    Never selects a registry pin (efficient/flash/mimo/planner handles) or any
    title we do not own. A job tab is finished when its `.exit` sidecar is at
    least `grace` seconds old.
    """
    out: list[dict] = []
    pinned = set(pinned_handles)
    for term in terminals or []:
        if not isinstance(term, dict):
            continue
        handle = str(term.get('handle') or '')
        title = str(term.get('title') or '').strip()
        if not handle or handle in pinned:
            continue
        job = parse_job_title(title)
        if job:
            role, dispatch_id = job
            exit_path = Path(packet_dir) / f'{dispatch_id}.exit'
            try:
                stamp = exit_path.stat().st_mtime
            except OSError:
                continue
            age = now - stamp
            if age >= grace:
                out = [*out, {'handle': handle, 'title': title, 'role': role,
                              'dispatch_id': dispatch_id, 'reason': f'job_exit:{dispatch_id}:{int(age)}'}]
            continue
        if is_shell_title(title):
            out = [*out, {'handle': handle, 'title': title, 'role': title.split('-')[0],
                          'dispatch_id': None, 'reason': 'orphan_packet_shell'}]
    return out


def close(handle: str, run=run_command) -> dict:
    out = run(['orca-ide', 'terminal', 'close', '--terminal', str(handle), '--tab', '--json'],
              timeout=15)
    return {'ok': bool(out.get('ok')), 'handle': handle, 'io': out}
