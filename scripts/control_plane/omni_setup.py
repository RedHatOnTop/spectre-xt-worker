"""Private on-box configuration for the Go proxy and free Flash profile."""
from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib

from .io import write_json, write_text

ENDPOINT = 'http://127.0.0.1:8790/v1'
FREE_MODEL = 'cline-free/cline-free/deepseek-v4.1-flash'
PAID_MODEL = 'cline-paid/cline-pass/deepseek-v4.1-flash'


def private_file(path: Path) -> None:
    if (not path.is_absolute() or path.is_symlink() or not path.is_file()
            or path.stat().st_mode & 0o077 or not path.read_text(encoding='utf-8').strip()):
        raise ValueError(f'{path.name} must be a nonempty private regular file')


def validate_credentials(path: Path) -> None:
    try:
        content = json.loads(path.read_text(encoding='utf-8'))
        auth = content['providers']['cline']['settings']['auth']
    except (ValueError, KeyError, TypeError):
        raise ValueError('Cline OAuth credential snapshot is invalid') from None
    if (not isinstance(auth, dict)
            or not all(isinstance(auth.get(name), str) and auth[name]
                       for name in ('accessToken', 'refreshToken'))
            or type(auth.get('expiresAt')) not in {int, float}):
        raise ValueError('Cline OAuth credential snapshot is invalid')


def proxy_config(credentials: Path) -> dict:
    return {
        'listen': '127.0.0.1:8790',
        'apiKeyEnv': 'OMNI_CLIENT_KEY',
        'providers': [
            {'id': 'cline-free', 'type': 'cline', 'baseUrl': 'https://api.cline.bot',
             'credentialsFile': str(credentials), 'credentialId': 'cline'},
            {'id': 'cline-paid', 'type': 'openai',
             'baseUrl': 'https://api.cline.bot/api/v1',
             'apiKeyEnv': 'CLINE_PASS_API_KEY'},
        ],
        'routing': {'order': ['cline-free', 'cline-paid'], 'fallback': True,
                    'fallbackOn429': True, 'maxAttempts': 2,
                    'retryStatuses': [401, 402, 403, 408, 409, 425, 429,
                                      500, 502, 503, 504, 522, 524, 529],
                    'fallbacks': {FREE_MODEL: [PAID_MODEL]}},
    }


def free_profile() -> str:
    return ('llm-pi-ai:\n'
            '  providers:\n'
            '    clinefree:\n'
            '      displayName: Cline Free via Omni\n'
            '      apiKeyEnv: CLINE_API_KEY\n'
            '      api: openai-completions\n'
            f'      baseURL: {ENDPOINT}\n'
            '      defaultInput: [text]\n'
            '      models:\n'
            f'        - id: {FREE_MODEL}\n'
            '          name: DeepSeek V4.1 Flash free-first\n'
            '\nagent-default-model:\n'
            '  provider: clinefree\n'
            f'  model: {FREE_MODEL}\n')


def prepare(home: Path, credentials: Path, paid_key: Path, client_key: str) -> dict:
    if not home.is_absolute() or not credentials.is_absolute() or not paid_key.is_absolute():
        raise ValueError('paths must be absolute')
    private_file(credentials)
    validate_credentials(credentials)
    private_file(paid_key)
    if not client_key or '\n' in client_key or '\x00' in client_key:
        raise ValueError('client key is invalid')
    config = proxy_config(credentials)
    root = home / '.config/omni-proxy'
    free_home = home / '.local/share/fullmoon-dsh-free'
    profiles = home / '.local/share/fullmoon-dsh/profiles'
    if not profiles.is_dir():
        raise ValueError('DSH profiles directory missing')
    existing = root / 'client_key'
    if existing.exists() and (existing.is_symlink() or existing.read_text().strip() != client_key):
        raise ValueError('proxy client key already exists with a different value')
    link = free_home / 'profiles'
    if link.is_symlink():
        if link.resolve() != profiles.resolve():
            raise ValueError('free DSH profiles link targets another directory')
    elif link.exists():
        raise ValueError('free DSH profiles path already exists')
    write_json(root / 'config.json', config)
    write_text(existing, client_key + '\n')
    write_text(free_home / 'settings.yaml', free_profile())
    if not link.is_symlink():
        link.symlink_to(profiles, target_is_directory=True)
    return {'config': config, 'config_path': str(root / 'config.json'),
            'free_profile_path': str(free_home / 'settings.yaml')}


def render_kimi(path: Path, client_key: str) -> str:
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ValueError('Kimi config must be a regular absolute file')
    original = path.read_text(encoding='utf-8')
    parsed = tomllib.loads(original)
    model = parsed.get('models', {}).get('cline/kimi-k3', {})
    provider = parsed.get('providers', {}).get('cline', {})
    if model.get('model') != 'cline-free/kimi-k3' or provider.get('type') != 'openai':
        raise ValueError('Kimi config must retain the Cline Kimi model')
    section = re.search(r'(?ms)^\[providers\.cline\]\s*\n(.*?)(?=^\[|\Z)', original)
    if section is None:
        raise ValueError('Kimi Cline provider section missing')
    replacement = section.group(1)
    replacement, base_count = re.subn(r'(?m)^base_url\s*=\s*.*$',
                                      f'base_url = {json.dumps(ENDPOINT)}', replacement)
    replacement, key_count = re.subn(r'(?m)^api_key\s*=\s*.*$',
                                     f'api_key = {json.dumps(client_key)}', replacement)
    if base_count != 1 or key_count != 1:
        raise ValueError('Kimi Cline provider must have one endpoint and key')
    updated = original[:section.start(1)] + replacement + original[section.end(1):]
    tomllib.loads(updated)
    return updated


def configure_kimi(path: Path, client_key: str) -> None:
    write_text(path, render_kimi(path, client_key))
