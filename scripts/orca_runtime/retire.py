"""Retire obsolete control-plane units without touching agent data or funding guards."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess

LEGACY_NAMES = (
    'spectre-worker-state', 'spectre-worker-state-watchdog', 'spectre-continuity',
    'spectre-loop', 'spectre-pin-sync', 'spectre-reaper', 'qoder-goal-watch',
    'qoder-continuity', 'qoder-nudge', 'qoder-idle-reaper', 'codex-goal-healer',
    'grokbot-goal-event', 'goal-supervisor', 'native-worker-pin-sync',
    'local-listener-reaper', 'slack-bridge', 'slack-brief', 'codex-provider-health',
)
LEGACY_UNITS = (
    *(f'{name}.{suffix}' for suffix in ('timer', 'service') for name in LEGACY_NAMES),
    'spectre-worker-state.socket', 'goal-supervisor.path', 'grokbot-goal-event.path',
)


def systemctl(*args):
    result = subprocess.run(['systemctl', '--user', *args], capture_output=True,
                            text=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError(f'systemctl --user {" ".join(args)} exited {result.returncode}\n'
                           f'{result.stdout}{result.stderr}')
    return result.stdout


def masked(path):
    return path.is_symlink() and path.readlink() == Path('/dev/null')


def plan(home):
    units = home / '.config/systemd/user'
    files = [str(units / name) for name in LEGACY_UNITS
             if (units / name).exists() and not masked(units / name)]
    registry = home / '.config/remote-agent/qoder-workers.json'
    return {'files': files, 'registry': str(registry) if registry.exists() else None,
            'units': list(LEGACY_UNITS), 'dry_run': True,
            'preserves': ['journals', 'workspaces', 'provider credentials', 'cost guards',
                          'thermal guard', 'backups', 'Discord intake', 'production watch']}


def apply(home, stamp=None):
    before = plan(home)
    stamp = stamp or datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = home / '.local/state/remote-agent/retired-runtime' / stamp
    if backup.exists():
        raise FileExistsError(f'Retirement archive already exists: {backup}')
    existing = [Path(f).name for f in before['files']]
    if existing:
        systemctl('disable', '--now', *existing)
    backup.mkdir(parents=True, mode=0o700)
    (backup / 'units').mkdir(mode=0o700)
    (backup / 'manifest.json').write_text(json.dumps(before, indent=2) + '\n')
    for filename in before['files']:
        source = Path(filename)
        source.rename(backup / 'units' / source.name)
    if before['registry']:
        Path(before['registry']).rename(backup / 'qoder-workers.json')
    units = home / '.config/systemd/user'
    units.mkdir(parents=True, exist_ok=True)
    for name in LEGACY_UNITS:
        target = units / name
        if not masked(target):
            target.symlink_to('/dev/null')
    systemctl('daemon-reload')
    return {**before, 'dry_run': False, 'backup': str(backup)}
