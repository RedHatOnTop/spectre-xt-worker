"""Pinned native DSH worker registration without changing existing agent identities."""
import hashlib
import json

from . import archive

CONFIG = 'out/shared/tui-agent-config.js'
MAIN_CONFIG = 'out/main/chunks/tui-agent-config-BVrI8OxM.js'
NAMES = 'out/shared/tui-agent-display-names.js'
MAIN_NAMES = 'out/main/chunks/tui-agent-display-names-DqHF1iQk.js'
KIND = 'out/shared/agent-kind.js'
ENUM = 'out/shared/telemetry-property-schemas.js'
MAIN = 'out/main/index.js'
READINESS = 'out/main/chunks/daemon-cgroup-scope-BJEqXHBB.js'
EDITS = (
    {'path': CONFIG, 'sha256': '27385a3138d1dd9532b71a27f06389f4f6e763239030ea167b32df9c22deabe5'},
    {'path': MAIN_CONFIG, 'sha256': 'b18b31076fb7d032dae39016490d93b40f16897c042c86f73ad88b65bb45422c'},
    {'path': NAMES, 'sha256': '0a683f9df744a0672b3538573551e93a552e5c27a35a2e0dba7c1b66ee452be6'},
    {'path': MAIN_NAMES, 'sha256': '8dca3f65906c12b203298e0e412d576e547958603f4f89ae41450089b52a91e6'},
    {'path': KIND, 'sha256': 'a78a2a884ceb1632588b09b0edec17bc2b930b2712a36faad0470dff874716e1'},
    {'path': ENUM, 'sha256': '513bcce0b25aad0d8b6b550c0d759d4b0f2c38888bdc173f8c73f91ecf7f8af0'},
    {'path': MAIN, 'sha256': '929e6dde694ab1140192d547076f4dccb2b0f05c3e73dacdc1b91680ec602b77'},
    {'path': READINESS, 'sha256': 'b6a16b98290c88a5c81e2a1b5530924262690711cf02ded07e64507406ec5ca5'},
)


def definitions():
    return {name: {'detectCmd': name, 'launchCmd': name,
                   'expectedProcess': name, 'promptInjectionMode': 'stdin-after-start'}
            for name, route in (('dsh-worker', 'clinepass'), ('dsh-worker-go', 'opencode-go'))}


def replace_exact(text, anchor, replacement):
    if text.count(anchor) != 1:
        raise ValueError('native registration anchor must occur exactly once')
    return text.replace(anchor, replacement)


def transform(path, body):
    expected = next(row['sha256'] for row in EDITS if row['path'] == path)
    if hashlib.sha256(body).hexdigest() != expected:
        raise ValueError(f'native registration source digest changed: {path}')
    text = body.decode()
    configs = ','.join(json.dumps(name) + ':' + json.dumps(row, separators=(',', ':'))
                       for name, row in definitions().items()) + ','
    if path == CONFIG:
        text = replace_exact(text, 'const TUI_AGENT_CONFIG_SOURCE = {', 'const TUI_AGENT_CONFIG_SOURCE = {' + configs)
    elif path == MAIN_CONFIG:
        text = replace_exact(text, 'var n={claude:', 'var n={' + configs + 'claude:')
    elif path == NAMES:
        text = replace_exact(text, "    muse: 'Muse',", "    muse: 'Muse',\n    'dsh-worker': 'DSH Worker',\n    'dsh-worker-go': 'DSH Worker Go',")
    elif path == MAIN_NAMES:
        text = replace_exact(text, 'const e={claude:', 'const e={"dsh-worker":"DSH Worker","dsh-worker-go":"DSH Worker Go",claude:')
    elif path == KIND:
        text = replace_exact(text, "    muse: 'muse'", "    muse: 'muse',\n    'dsh-worker': 'dsh-worker',\n    'dsh-worker-go': 'dsh-worker-go'")
    elif path == ENUM:
        text = replace_exact(text, "    'muse',\n    'other'", "    'muse',\n    'dsh-worker',\n    'dsh-worker-go',\n    'other'")
    elif path == MAIN:
        text = replace_exact(text, 'muse:`muse`};function', 'muse:`muse`,"dsh-worker":"dsh-worker","dsh-worker-go":"dsh-worker-go"};function')
        text = replace_exact(text, '.trae.muse.other`.split', '.trae.muse.dsh-worker.dsh-worker-go.other`.split')
    elif path == READINESS:
        text = replace_exact(text, 'function xb(e){if(!e||',
            'function xb(e){if(/^DSH Worker(?: Go)? ready$/.test(e))return`idle`;'
            'if(/^DSH Worker(?: Go)? working$/.test(e))return`working`;'
            'if(/^DSH Worker(?: Go)? blocked$/.test(e))return`permission`;if(!e||')
    return text.encode()


def build(source, target):
    changes = {row['path']: transform(row['path'], archive.read(source, row['path'])) for row in EDITS}
    return archive.patch(source, target, changes)
