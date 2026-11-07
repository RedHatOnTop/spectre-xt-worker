"""An isolated Go profile pins the existing subscription route without fallback."""
import json
import os
from pathlib import Path

GO_ENDPOINT = 'https://opencode.ai/inference/go/v1'
GO_MODEL = 'glm-5.3-flash'
GO_PROVIDER = 'opencode-go'
GO_KEY_ENV = 'OPENCODE_GO_API_KEY'


def go_profile():
    return {'llm-pi-ai': {'providers': {GO_PROVIDER: {
                'displayName': 'OpenCode Go isolated worker', 'apiKeyEnv': GO_KEY_ENV,
                'api': 'openai-completions', 'baseURL': GO_ENDPOINT,
                'defaultInput': ['text'], 'models': [{'id': GO_MODEL}]}}},
            'agent-default-model': {'provider': GO_PROVIDER, 'model': GO_MODEL}}


def validate_go(home):
    path = Path(home) / 'settings.yaml'
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise ValueError('Go DSH profile must be a private regular file')
    data = json.loads(path.read_text(encoding='utf-8'))
    provider = data.get('llm-pi-ai', {}).get('providers', {})
    if set(provider) != {GO_PROVIDER}:
        raise ValueError('Go DSH profile must contain only its direct provider')
    route = provider[GO_PROVIDER]
    if (route.get('baseURL') != GO_ENDPOINT or route.get('api') != 'openai-completions'
            or route.get('apiKeyEnv') != GO_KEY_ENV or route.get('models') != [{'id': GO_MODEL}]
            or data.get('agent-default-model') != {'provider': GO_PROVIDER, 'model': GO_MODEL}):
        raise ValueError('Go DSH profile changed its approved route or model')
    return data


def write_once(path, body):
    if path.exists() or path.is_symlink():
        if path.is_symlink() or path.read_text() != body:
            raise ValueError('existing Go staging file conflicts with reviewed content')
        return
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, 'w', encoding='utf-8') as output:
        output.write(body)
        output.flush()
        os.fsync(output.fileno())


def stage_go(base, home, profile_patch=None):
    base, home = Path(base).absolute(), Path(home).absolute()
    if base != base.resolve() or home != home.resolve() or home == base:
        raise ValueError('Go staging requires distinct non-symlink runtime homes')
    profile = base / 'profiles/headless'
    modules = base / 'profiles/node_modules'
    if not modules.is_dir() or not (profile / 'package.json').is_file():
        raise ValueError('existing DSH headless runtime dependencies are missing')
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    target = home / 'profiles/headless'
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_once(home / 'settings.yaml', json.dumps(go_profile(), indent=2) + '\n')
    write_once(target / 'package.json', (profile / 'package.json').read_text())
    patch = profile / 'cordis.patch.yml'
    if profile_patch is not None or patch.is_file():
        write_once(target / 'cordis.patch.yml', profile_patch if profile_patch is not None else patch.read_text())
    link = home / 'profiles/node_modules'
    if link.is_symlink():
        if link.resolve() != modules.resolve():
            raise ValueError('Go runtime dependency link targets another profile')
    elif link.exists():
        raise ValueError('Go runtime dependency path conflicts with existing content')
    else:
        link.symlink_to(modules, target_is_directory=True)
    validate_go(home)
    return {'home': str(home), 'provider': GO_PROVIDER, 'model': GO_MODEL, 'endpoint': GO_ENDPOINT}
