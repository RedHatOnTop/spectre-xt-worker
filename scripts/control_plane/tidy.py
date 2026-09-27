"""Select and close finished Orca packet tabs. Dry-run is selection only.

A tab belongs to the control plane only if the bridge recorded it at create
time as `<packet_dir>/tabs/<handle>.json`. Titles are never evidence: the
shell's OSC title replaces `--title` within seconds and agents set their own.
"""
from __future__ import annotations

import json
from pathlib import Path
import re

from .io import run as run_command

TAB_HANDLE = re.compile(r'term_[A-Za-z0-9_-]{1,96}')
DISPATCH_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}')
GRACE_SEC = 300


def records(packet_dir: Path) -> dict[str, dict]:
    """Tab records keyed by handle. Malformed files are skipped, never removed."""
    try:
        paths = sorted((Path(packet_dir) / 'tabs').glob('*.json'))
    except OSError:
        return {}
    out: dict[str, dict] = {}
    for path in paths:
        handle = path.name[:-len('.json')]
        if not TAB_HANDLE.fullmatch(handle):
            continue
        try:
            record = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if isinstance(record, dict) and record.get('handle') == handle:
            out = {**out, handle: record}
    return out


def select(terminals: list[dict], pinned_handles: set[str], packet_dir: Path,
           now: float, grace: float = GRACE_SEC) -> list[dict]:
    """Recorded job tabs whose `.exit` sidecar is at least `grace` seconds old.

    Never selects a registry pin or an unrecorded tab. Recorded packet shells
    are kept because the next dispatch reuses them.
    """
    owned = records(packet_dir)
    pinned = set(pinned_handles)
    out: list[dict] = []
    for term in terminals or []:
        if not isinstance(term, dict):
            continue
        handle = str(term.get('handle') or '')
        record = owned.get(handle)
        if handle in pinned or not record or record.get('kind') != 'job':
            continue
        dispatch_id = str(record.get('dispatch_id') or '')
        if not DISPATCH_ID.fullmatch(dispatch_id):
            continue
        try:
            stamp = (Path(packet_dir) / f'{dispatch_id}.exit').stat().st_mtime
        except OSError:
            continue
        age = now - stamp
        if age >= grace:
            out = [*out, {'handle': handle, 'role': record.get('role'), 'dispatch_id': dispatch_id,
                          'reason': f'job_exit:{dispatch_id}:{int(age)}'}]
    return out


def stale(terminals: list[dict], packet_dir: Path, now: float,
          grace: float = GRACE_SEC) -> list[dict]:
    """Records whose tab is gone from the Orca listing.

    `grace` covers a record the bridge wrote after `terminals` was listed.
    """
    listed = {str(term.get('handle') or '') for term in terminals or [] if isinstance(term, dict)}
    out: list[dict] = []
    for handle, record in records(packet_dir).items():
        created = record.get('created_at')
        if handle in listed or not isinstance(created, (int, float)) or now - created < grace:
            continue
        out = [*out, {'handle': handle, 'role': record.get('role'), 'kind': record.get('kind'),
                      'dispatch_id': record.get('dispatch_id'), 'reason': 'tab_gone'}]
    return out


def retire(handle: str, packet_dir: Path) -> dict:
    if not TAB_HANDLE.fullmatch(str(handle)):
        return {'ok': False, 'handle': handle, 'error': 'bad_handle'}
    try:
        (Path(packet_dir) / 'tabs' / f'{handle}.json').unlink(missing_ok=True)
    except OSError as exc:
        return {'ok': False, 'handle': handle, 'error': f'record_retire_failed: {exc.strerror}'}
    return {'ok': True, 'handle': handle}


# A full-screen TUI (DSH, qodercli, an editor) enables SGR mouse reporting and
# normally turns it off on exit. A tab whose process was killed instead keeps
# reporting, and every mouse movement over it is typed into the shell as
# `ESC[<b;x;yM` (rendered as `35;96;1M…`). Closing such a tab is the moment the
# pollution becomes permanent, so reset the modes first.
TERM_RESET_LINE = ('printf "\\033[?1000l\\033[?1002l\\033[?1003l\\033[?1004l'
                   '\\033[?1006l\\033[?1049l\\033[?25h"; stty sane 2>/dev/null; '
                   'tput sgr0 2>/dev/null; clear')


def reset(handle: str, run=run_command) -> dict:
    """Best-effort: clear a terminal a killed TUI left in mouse/alt-screen mode."""
    if not TAB_HANDLE.fullmatch(str(handle)):
        return {'ok': False, 'handle': handle, 'error': 'bad_handle'}
    run(['orca-ide', 'terminal', 'send', '--terminal', str(handle), '--interrupt'], timeout=15)
    run(['orca-ide', 'terminal', 'send', '--terminal', str(handle), '--text',
         TERM_RESET_LINE, '--enter'], timeout=15)
    return {'ok': True, 'handle': handle}


def close(handle: str, run=run_command) -> dict:
    reset(handle, run)
    out = run(['orca-ide', 'terminal', 'close', '--terminal', str(handle), '--tab', '--json'],
              timeout=15)
    if not out.get('ok'):
        return {'ok': False, 'handle': handle, 'io': out, 'error': 'orca_close_failed'}
    return {'ok': True, 'handle': handle, 'io': out}


def sweep(terminals: list[dict], pinned_handles: set[str], packet_dir: Path, now: float, *,
          apply: bool, truncated: bool, run=run_command) -> tuple[list[dict], list[dict]]:
    """Close finished job tabs and retire records whose tab is gone.

    A closed tab's record is retired by a later sweep, once the listing no
    longer shows it.
    """
    closed = [{**row, **close(row['handle'], run)} if apply else {**row, 'ok': True, 'dry_run': True}
              for row in select(terminals, pinned_handles, packet_dir, now)]
    # A truncated listing is not proof that a tab is gone.
    gone = [] if truncated else stale(terminals, packet_dir, now)
    retired = [{**row, **retire(row['handle'], packet_dir)} if apply else {**row, 'ok': True, 'dry_run': True}
               for row in gone]
    return closed, retired
