"""Private atomic state and bounded external commands."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import subprocess
import tempfile


def read_json(path: Path) -> dict:
    try:
        text = path.read_text(encoding='utf-8')
    except FileNotFoundError:
        return {}
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError(f'{path.name}: expected an object')
    return value


def write_json(path: Path, value: dict) -> None:
    write_text(path, json.dumps(value, indent=2, sort_keys=True) + '\n')


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(name).unlink(missing_ok=True)


@contextmanager
def locked(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise BlockingIOError(exc.errno, 'lock is held', str(path)) from None
        yield
    finally:
        os.close(fd)


def run(argv: list[str], *, environ: dict | None = None, timeout: float = 8,
        cwd: str | None = None) -> dict:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False, cwd=cwd,
                              timeout=timeout, env={**os.environ, **(environ or {})})
    except subprocess.TimeoutExpired:
        return {'ok': False, 'uncertain': True, 'error': f'{Path(argv[0]).name}: timeout'}
    except OSError as exc:
        return {'ok': False, 'error': f'{Path(argv[0]).name}: {exc.strerror}'}
    try:
        parsed = json.loads(proc.stdout.strip()) if proc.stdout.strip() else {}
    except json.JSONDecodeError:
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    ok = proc.returncode == 0 and not (isinstance(parsed, dict) and parsed.get('ok') is False)
    return {'ok': ok, 'code': proc.returncode, 'parsed': parsed,
            'error': None if ok else f'{Path(argv[0]).name}: exit {proc.returncode}'}


def checked(response: tuple[int, dict]) -> dict:
    status, value = response
    if status != 200 or value.get('ok') is False:
        raise RuntimeError(f"state API {status}: {value.get('error', 'request failed')}")
    return value
