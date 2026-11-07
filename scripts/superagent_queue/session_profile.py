"""Stage a pinned core-session runner without replacing headless or providers."""
import hashlib
import json
from pathlib import Path

from .execution import private_directory
from .routes import write_once

SOURCE_SHA256 = 'ef581ed89350c009943a94e61379eace98341877943da36916501e14244b185b'
PROFILE = 'spectre-worker-session-v3'
PACKAGE = 'spectre-dsh-worker-runner'
GO_PACKAGE = 'spectre-dsh-go-session'
GO_FILES = ('index.js', 'session.mjs', 'package.json')


def package_manifest():
    return json.dumps({'name': PACKAGE, 'private': True, 'type': 'module',
                       'exports': './index.js'}, indent=2) + '\n'


def overlay(patch):
    if PACKAGE in patch:
        raise ValueError('original profile already defines the session runner')
    return patch.rstrip() + ('\n\n- id: headless-runner\n'
                             '  name: "@deepseek-ai/dsh-headless"\n  disabled: true\n'
                             '- insert:\n    - id: spectre-session-runner\n'
                             f'      name: {PACKAGE}\n      inject: [headlessStartup]\n'
                             '      config:\n        task: !!js ctx.headlessStartup.task\n')


def render(source):
    if hashlib.sha256(source.encode()).hexdigest() != SOURCE_SHA256:
        raise ValueError('session runner does not match the pinned headless source')
    replacements = (
        ('import { randomUUID } from "node:crypto";\n', 'import { openAgent, authorize } from "./session.mjs";\n'),
        ('import { brandString } from "@deepseek-ai/dsh-brand";\n', ''),
        ('const { agent } = await agents.create({', 'const { agent } = await openAgent(agents, {'),
        ('\t\tsessionId: brandString(`session-${randomUUID()}`),\n', ''),
        ('\t});\n\tawait agent.whenIdle();\n', '\t}, process.env);\n\tawait agent.whenIdle();\n'),
        ('\t\tagent.followup(createUserMessage({', '\t\tawait authorize(process.env);\n\t\tagent.followup(createUserMessage({'),
    )
    result = source
    for anchor, replacement in replacements:
        if result.count(anchor) != 1:
            raise ValueError('pinned session runner anchor must occur exactly once')
        result = result.replace(anchor, replacement)
    return result


def local_adapter(original):
    patch = (original / 'cordis.patch.yml').read_text()
    if f'name: {GO_PACKAGE}' not in patch:
        return {}
    package = original / 'node_modules' / GO_PACKAGE
    paths = tuple(package / name for name in GO_FILES)
    if any(path.absolute() != path.resolve() for path in paths):
        raise ValueError('session provider adapter cannot contain symlinks')
    return {path.name: path.read_text() for path in paths}


def stage(home, source, helper):
    home = Path(home).absolute()
    if home != home.resolve():
        raise ValueError('session profile home cannot contain symlinks')
    original = home / 'profiles/headless'
    target = private_directory(home / 'profiles' / PROFILE)
    package = private_directory(target / 'node_modules' / PACKAGE)
    body = render(Path(source).read_text())
    patch = (original / 'cordis.patch.yml').read_text()
    adapter = local_adapter(original)
    write_once(target / 'package.json', (original / 'package.json').read_text())
    write_once(target / 'cordis.patch.yml', overlay(patch))
    write_once(package / 'index.js', body)
    write_once(package / 'session.mjs', Path(helper).read_text())
    write_once(package / 'package.json', package_manifest())
    write_once(package / 'LICENSE', Path(source).with_name('LICENSE').read_text())
    if adapter:
        target_adapter = private_directory(target / 'node_modules' / GO_PACKAGE)
        for name, content in adapter.items():
            write_once(target_adapter / name, content)
    return {'profile': PROFILE, 'home': str(home), 'source_sha256': SOURCE_SHA256,
            'runner_sha256': hashlib.sha256(body.encode()).hexdigest(),
            'helper_sha256': hashlib.sha256(Path(helper).read_bytes()).hexdigest()}


def verify(home, source, helper):
    target = Path(home) / 'profiles' / PROFILE
    package = target / 'node_modules' / PACKAGE
    if Path(home).absolute() != Path(home).resolve():
        raise ValueError('session profile home cannot contain symlinks')
    expected = {package / 'index.js': render(Path(source).read_text()),
                package / 'session.mjs': Path(helper).read_text(),
                package / 'package.json': package_manifest(),
                package / 'LICENSE': Path(source).with_name('LICENSE').read_text(),
                target / 'package.json': (Path(home) / 'profiles/headless/package.json').read_text()}
    adapter = local_adapter(Path(home) / 'profiles/headless')
    expected = {**expected, **{target / 'node_modules' / GO_PACKAGE / name: content
                               for name, content in adapter.items()}}
    for path, body in expected.items():
        if path.absolute() != path.resolve() or path.read_text() != body:
            raise ValueError('session profile does not match the reviewed runner')
    patch = (target / 'cordis.patch.yml').read_text()
    original = (Path(home) / 'profiles/headless/cordis.patch.yml').read_text()
    if patch != overlay(original):
        raise ValueError('session profile does not match the reviewed provider overlay')
    return {'profile': PROFILE, 'home': str(home)}
