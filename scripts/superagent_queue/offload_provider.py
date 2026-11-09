"""Query or stop only the configured existing CPU Studio; never start or change funding."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess

ENV_FILE = Path.home() / '.config/remote-agent/lightning.env'
CLI = Path('/usr/local/bin/lightning')
CONFIGURE = '''set -euo pipefail
source "$1"
if [[ "${LIGHTNING_STUDIO:-}" != "$3" || "${LIGHTNING_TEAMSPACE:-}" != "$4" || "${LIGHTNING_MACHINE:-CPU}" != CPU ]]; then
  printf '%s\\n' 'offload provider configuration no longer matches original Studio' >&2
  exit 2
fi
exec "$2" studio "$5" "${@:6}"
'''


def query(target, action):
    if (action not in ('status', 'stop') or not isinstance(target.get('studio'), str)
            or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', target['studio'])
            or not isinstance(target.get('teamspace'), str) or not target['teamspace']):
        raise ValueError('resource operation requires exact original Studio and supported action')
    if ENV_FILE.absolute() != ENV_FILE.resolve():
        raise ValueError('offload provider configuration cannot contain symlinks')
    entry = ENV_FILE.stat()
    if not stat.S_ISREG(entry.st_mode) or entry.st_uid != os.getuid() or stat.S_IMODE(entry.st_mode) != 0o600:
        raise ValueError('offload provider configuration must be private and owned')
    env = {key: value for key, value in os.environ.items() if not key.startswith('LIGHTNING_')}
    words = (['list', '--teamspace', target['teamspace'], '--json'] if action == 'status'
             else ['stop', '--name', target['studio'], '--teamspace', target['teamspace']])
    command = ['/bin/bash', '-c', CONFIGURE, 'offload-provider', str(ENV_FILE), str(CLI),
               target['studio'], target['teamspace'], *words]
    result = subprocess.run(command, env=env, text=True, capture_output=True, timeout=60, check=False)
    if result.returncode:
        raise RuntimeError(f'offload provider {action} exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}')
    if action == 'stop':
        return {'stop_requested': True}
    rows = json.loads(result.stdout)
    selected = [row for row in rows if isinstance(row, dict) and row.get('name') == target['studio']] if isinstance(rows, list) else []
    if len(selected) != 1 or selected[0].get('status') not in ('Running', 'Pending', 'Stopped'):
        raise ValueError('offload provider returned unknown Studio status')
    status = selected[0]['status']
    if status != 'Running':
        return {'status': status}
    result = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '-o', 'LogLevel=ERROR',
                             target['studio'], 'cat /proc/sys/kernel/random/boot_id'],
                            env=env, text=True, capture_output=True, timeout=30, check=False)
    if result.returncode:
        raise RuntimeError(f'offload Studio boot query exited {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}')
    boot = result.stdout.strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', boot):
        raise ValueError('offload Studio boot identity cannot be verified')
    return {'status': status, 'remote_boot_id': boot}
