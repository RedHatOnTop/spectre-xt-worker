#!/usr/bin/env python3
"""Install a verified private harness control closure without restarting sessions."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

BEGIN = '<!-- harness-workers:begin -->'
END = '<!-- harness-workers:end -->'
RULE = '''Use one Orca coordinator and at most two implementation workers per Run.
Designated harnesses are DSH ClinePass (`dsh-worker`) and existing OpenCode Go
(`dsh-worker-go`), without new funding or paid fallback. Delegate implementation
and routine verification to these workers; the coordinator owns decomposition,
decisions and final evidence review. Do not launch additional Agentrouter workers.
Load the installed `harness-workers` skill and exact host Orca orchestration guide.
Use `harness-workers start --apply` from the native coordinator pane for bounded
implementation starts. Raw starts are not intercepted; do not bypass this path.
Native Run/Task/Dispatch owns execution, not the adapter's launch receipt journal.
Unknown starts/liveness retain capacity and require original-request recovery.
Never kill/restart existing sessions to free a slot or evade a wait. Older Orca
interfaces require a verified session-preserving update, not weaker identity
checks. Existing production, publication, cost and device-trust approvals remain.
'''


def digest_sources(source):
    files = [source / 'scripts/harness-workers.py', source / 'scripts/spectre-dsh-worker.py',
             source / 'config/harness-workers.json',
             source / 'config/harness-workers-skill.md',
             *sorted((source / 'scripts/superagent_queue').glob('*.py')),
             *sorted((source / 'scripts/orca_runtime').glob('*.py'))]
    digest = hashlib.sha256()
    for file in files:
        if file.is_symlink() or not file.is_file():
            raise ValueError('harness runtime source must be a regular non-symlink file')
        digest.update(str(file.relative_to(source)).encode() + b'\0' + file.read_bytes())
    return digest.hexdigest()


def preflight(home, source):
    command = [sys.executable, '-I', '-B', '-c',
        'import sys;sys.path.insert(0,sys.argv[1]);from superagent_queue import worker_pool,worker_launch',
        str(source / 'scripts')]
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=20)
    if result.returncode:
        raise RuntimeError(f'harness source import exited {result.returncode}\n{result.stdout}{result.stderr}')
    policy = home / '.config/remote-agent/harness-workers.json'
    if policy.exists() and (policy.is_symlink() or policy.read_bytes() != (source / 'config/harness-workers.json').read_bytes()):
        raise ValueError('existing harness worker policy differs; review it before installation')
    target = home / '.local/bin/harness-workers'
    if target.exists() or target.is_symlink():
        if not target.is_symlink() or not target.resolve().is_relative_to(home / '.local/share/harness-workers'):
            raise ValueError('existing harness worker command is not owned by this installer')
    for path in (home / '.codex', home / '.codex/AGENTS.md',
                 home / '.agents/skills/harness-workers', home / '.agents/skills/harness-workers/SKILL.md'):
        if path.is_symlink():
            raise ValueError('harness memory/skill path cannot be a symlink')
    target = home / '.codex/AGENTS.md'
    text = target.read_text() if target.exists() else ''
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise ValueError('existing harness worker memory markers are malformed')


def verify_stage(root, source):
    files = [(source / 'scripts/harness-workers.py', root / 'bin/harness-workers'),
             (source / 'scripts/spectre-dsh-worker.py', root / 'bin/spectre-dsh-worker')]
    for package in ('superagent_queue', 'orca_runtime'):
        files = [*files, *((path, root / 'lib/spectre-superagent' / package / path.name)
                 for path in sorted((source / 'scripts' / package).glob('*.py')))]
    for before, after in files:
        if after.is_symlink() or not after.is_file() or before.read_bytes() != after.read_bytes():
            raise ValueError('installed harness runtime differs from its verified source closure')


def stage(home, source, digest):
    root = home / '.local/share/harness-workers' / digest
    if root.exists():
        verify_stage(root, source)
        return root
    root.mkdir(parents=True, mode=0o700)
    library = root / 'lib/spectre-superagent'
    library.mkdir(parents=True)
    for package in ('superagent_queue', 'orca_runtime'):
        shutil.copytree(source / 'scripts' / package, library / package, ignore=shutil.ignore_patterns('__pycache__'))
    (root / 'bin').mkdir()
    shutil.copy2(source / 'scripts/harness-workers.py', root / 'bin/harness-workers')
    (root / 'bin/harness-workers').chmod(0o755)
    shutil.copy2(source / 'scripts/spectre-dsh-worker.py', root / 'bin/spectre-dsh-worker')
    (root / 'bin/spectre-dsh-worker').chmod(0o755)
    command = [sys.executable, '-I', '-B', '-c',
        'import sys;sys.path.insert(0,sys.argv[1]);from superagent_queue import worker_pool,worker_launch', str(library)]
    result = subprocess.run(command, capture_output=True, text=True, check=False, timeout=20)
    if result.returncode:
        raise RuntimeError(f'harness staged import exited {result.returncode}\n{result.stdout}{result.stderr}')
    verify_stage(root, source)
    return root


def memory(home):
    target = home / '.codex/AGENTS.md'
    target.parent.mkdir(parents=True, exist_ok=True)
    text = target.read_text() if target.exists() else ''
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise ValueError('existing harness worker memory markers are malformed')
    block = BEGIN + '\n' + RULE + END
    updated = text[:text.index(BEGIN)] + block + text[text.index(END) + len(END):] if BEGIN in text else text + '\n' + block + '\n'
    if updated == text:
        return None
    backup = target.with_name('AGENTS.md.harness-workers-' + str(time.time_ns()))
    backup.write_text(text)
    backup.chmod(0o600)
    temporary = target.with_name('.AGENTS.md.harness-workers-' + str(time.time_ns()))
    temporary.write_text(updated)
    temporary.chmod(0o600)
    os.replace(temporary, target)
    return str(backup)


def reader_bytes(home, source, expected):
    reader = home / '.local/lib/spectre-superagent/scripts/spectre-dsh-worker.py'
    if not reader.exists() and not reader.is_symlink():
        return None
    if reader.is_symlink() and not reader.resolve().is_relative_to(home / '.local/share/harness-workers'):
        raise ValueError('worker reader symlink is not owned by this installer')
    if not reader.is_file() or reader.stat().st_uid != os.getuid():
        raise ValueError('worker reader must be an owned regular file')
    body = reader.read_bytes()
    if body != (source / 'scripts/spectre-dsh-worker.py').read_bytes() and hashlib.sha256(body).hexdigest() != expected:
        raise ValueError('worker reader changed or lacks its expected original digest')
    return body


def promote_reader(home, root, original):
    if original is None:
        return None
    reader = home / '.local/lib/spectre-superagent/scripts/spectre-dsh-worker.py'
    target = root / 'bin/spectre-dsh-worker'
    if reader.is_symlink() and reader.resolve() == target.resolve():
        return None
    if reader.read_bytes() != original:
        raise ValueError('worker reader changed during installation')
    backup = home / '.local/state/remote-agent/worker-updates' / str(time.time_ns())
    backup.mkdir(parents=True, mode=0o700)
    saved = backup / 'spectre-dsh-worker.py'
    saved.write_bytes(original)
    saved.chmod(0o600)
    temporary = reader.with_name('.spectre-dsh-worker-' + str(time.time_ns()))
    temporary.symlink_to(target)
    os.replace(temporary, reader)
    return str(saved)


def install(home, source, expected_reader_sha256=None):
    home, source = Path(home).absolute(), Path(source).absolute()
    if home != home.resolve() or source != source.resolve():
        raise ValueError('harness install home/source cannot contain symlinks')
    preflight(home, source)
    original_reader = reader_bytes(home, source, expected_reader_sha256)
    digest = digest_sources(source)
    root = stage(home, source, digest)
    reader_backup = promote_reader(home, root, original_reader)
    policy = home / '.config/remote-agent/harness-workers.json'
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_bytes((source / 'config/harness-workers.json').read_bytes())
    policy.chmod(0o600)
    skill = home / '.agents/skills/harness-workers/SKILL.md'
    skill.parent.mkdir(parents=True, exist_ok=True)
    skill.write_bytes((source / 'config/harness-workers-skill.md').read_bytes())
    skill.chmod(0o600)
    backup = memory(home)
    command = home / '.local/bin/harness-workers'
    command.parent.mkdir(parents=True, exist_ok=True)
    if not command.is_symlink() or command.resolve() != root / 'bin/harness-workers':
        temporary = command.with_name('.harness-workers-' + str(time.time_ns()))
        temporary.symlink_to(root / 'bin/harness-workers')
        os.replace(temporary, command)
    if original_reader is None:
        refresh_worker_library(home, root)
    return {'runtime_sha256': digest, 'runtime': str(root), 'memory_backup': backup, 'reader_backup': reader_backup,
            'session_restart': False, 'native_start': False}


def refresh_worker_library(home, runtime):
    """Point an already-installed worker layout at this snapshot. Do not create one."""
    stable = home / '.local/lib/spectre-superagent'
    if not stable.exists():
        return None
    if stable.is_symlink() or not stable.is_dir():
        raise ValueError('worker library root cannot be a symlink')
    target = runtime / 'lib/spectre-superagent'
    if not target.is_dir() or target.is_symlink():
        raise ValueError('harness runtime is missing its worker library')
    link = stable / 'lib/spectre-superagent'
    if link.is_symlink() and link.resolve() == target.resolve():
        return str(link)
    if link.exists() and not link.is_symlink():
        raise ValueError('worker library link is a real directory')
    link.parent.mkdir(parents=True, exist_ok=True)
    temporary = link.with_name('.spectre-superagent-' + str(time.time_ns()))
    temporary.symlink_to(target)
    os.replace(temporary, link)
    return str(link)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home())
    parser.add_argument('--source', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--expected-reader-sha256')
    args = parser.parse_args()
    try:
        print(json.dumps(install(args.home, args.source, args.expected_reader_sha256), indent=2))
        return 0
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as error:
        print(f'harness worker install: {error}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
