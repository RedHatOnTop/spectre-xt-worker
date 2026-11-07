"""Reproducible session propagation patch for one isolated pinned DSH adapter."""
import hashlib
import json
from pathlib import Path

from .routes import write_once

SOURCE_SHA256 = '1f787eb5cd3d0308e7a2563cdb2b8cc2c1fc153fe06b55d39439fb2446059483'
ANCHOR = 'headers: requestHeaders(profile.headers)'
SNAPSHOT_PI_AI = 'file:///snapshot/deepseek-harness/python/sdk-runtime/src/deepseek_harness_runtime/runtime/node/node_modules/@earendil-works/pi-ai/'
SNAPSHOT_IMPORTS = (
    ('@earendil-works/pi-ai/providers/all', 'dist/providers/all.js'),
    ('@earendil-works/pi-ai/api/anthropic-messages.lazy', 'dist/api/anthropic-messages.lazy.js'),
    ('@earendil-works/pi-ai/api/openai-completions.lazy', 'dist/api/openai-completions.lazy.js'),
    ('@earendil-works/pi-ai/api/openai-responses.lazy', 'dist/api/openai-responses.lazy.js'),
)


def render(source):
    if source.count(ANCHOR) != 1:
        raise ValueError('pinned Go header anchor must occur exactly once')
    replacement = ('headers: requestHeaders(options.provider === "opencode-go" '
                   '? { ...profile.headers, ...goSessionHeaders(options.sessionId) } : profile.headers)')
    return 'import { goSessionHeaders } from "./session.mjs";\n' + source.replace(ANCHOR, replacement)


def snapshot_imports(source):
    result = source
    for name, path in SNAPSHOT_IMPORTS:
        anchor = f'from "{name}"'
        if result.count(anchor) != 1:
            raise ValueError('pinned runtime import must occur exactly once')
        result = result.replace(anchor, f'from "{SNAPSHOT_PI_AI}{path}"')
    return result


def overlay(base):
    if 'llm-pi-ai' in base or 'spectre-go-pi-ai' in base:
        raise ValueError('base profile already configures an existing adapter')
    return (base.rstrip() + '\n\n- id: llm-pi-ai\n'
            '  name: "@deepseek-ai/dsh-llm-pi-ai"\n  disabled: true\n'
            '- insert:\n    - id: spectre-go-pi-ai\n      name: spectre-dsh-go-session\n')


def stage(source, target, helper):
    source, target, helper = Path(source), Path(target), Path(helper)
    body = source.read_bytes()
    if hashlib.sha256(body).hexdigest() != SOURCE_SHA256:
        raise ValueError('Go adapter does not match the pinned source digest')
    rendered = snapshot_imports(render(body.decode('utf-8')))
    session = helper.read_text()
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    write_once(target / 'index.js', rendered)
    write_once(target / 'session.mjs', session)
    write_once(target / 'package.json', json.dumps({'name': 'spectre-dsh-go-session',
               'private': True, 'type': 'module', 'exports': './index.js'}, indent=2) + '\n')
    return {'source_sha256': SOURCE_SHA256,
            'patched_sha256': hashlib.sha256(rendered.encode()).hexdigest(), 'target': str(target)}
