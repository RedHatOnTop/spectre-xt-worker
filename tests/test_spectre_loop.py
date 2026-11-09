"""CLI integration: real UDS state daemon and bridge, fake Orca terminal I/O."""
from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from control_plane import runtime
from control_plane.io import read_json, write_json
from worker_state.client import StateClient


class LoopCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.packet_dir = self.root / 'packets'
        self.packet_dir.mkdir()
        self.proc = self.root / 'proc'
        self.proc.mkdir()
        self.workers = self.root / 'workers.json'
        self.state = self.root / 'loop.json'
        self.sent = self.root / 'sent.jsonl'
        self.socket = self.root / 'state.sock'
        self.client = StateClient(str(self.socket))
        self.env = {**os.environ, 'HOME': str(self.root), 'SPECTRE_LOOP': '1', 'ASTRA_ENABLED': '1',
            'SPECTRE_LOOP_STATE': str(self.state), 'SPECTRE_PACKET_DIR': str(self.packet_dir),
            'SPECTRE_PROC_ROOT': str(self.proc), 'SPECTRE_WORKER_STATE_SOCK': str(self.socket),
            'SPECTRE_DISPATCH_BIN': str(self.bin / 'bridge'), 'SPECTRE_NOTIFY_BIN': str(self.bin / 'notify'),
            'SPECTRE_PROVIDER_STATE': str(self.root / 'provider.json'),
            'SPECTRE_QODER_WORKERS': str(self.workers), 'QODER_WORKERS_FILE': str(self.workers),
            'PATH': str(self.bin) + ':' + os.environ['PATH']}
        self.fake_process(101, ['codex', '-m', 'gpt-6-astra'], 'term_astra')
        self.fake_process(102, ['qodercli', '-m', 'Efficient'], 'term_eff')
        self.fake_process(103, ['bash'], 'term_flash')
        entry = {'cwd': str(self.root), 'terminal': 'term_eff', 'tmux': None,
                 'planner': {'terminal': 'term_astra', 'provider': 'anyrouter'},
                 'targets': {'flash': {'terminal': 'term_flash'}}}
        write_json(self.workers, {'workers': {'minecraft': entry}})
        write_json(self.root / 'provider.json', {'ok': True, 'id': 'anyrouter', 'checked_at': time.time()})
        self.script('bridge', f'#!/bin/sh\nexec node {ROOT / "scripts/slack-bridge.mjs"} "$@" --workers-file {self.workers}\n')
        self.script('notify', '#!/bin/sh\nexit 0\n')
        terms = [{'handle': handle, 'worktreePath': str(self.root), 'connected': True, 'writable': True}
                 for handle in ('term_astra', 'term_eff', 'term_flash')]
        self.script('orca-ide', f'#!{sys.executable}\nimport json,sys\n'
            f'if sys.argv[1:3] == ["terminal","list"]: print({json.dumps(json.dumps({"ok": True, "result": {"terminals": terms}}))})\n'
            'else:\n'
            f' with open({str(self.sent)!r}, "a") as out: out.write(json.dumps(sys.argv[1:]) + "\\n")\n'
            ' print("{\\"ok\\":true}")\n')
        self.daemon = subprocess.Popen([sys.executable, str(ROOT / 'scripts/spectre-state.py'),
            '--socket', str(self.socket), '--db', str(self.root / 'worker.sqlite'),
            'serve', '--poll-interval', '0', '--no-shadow'], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=self.env)
        for _ in range(100):
            if self.client.health()[0] == 200:
                break
            time.sleep(0.02)
        health = self.client.health()
        if health[0] != 200:
            self.daemon.terminate()
            stdout, stderr = self.daemon.communicate(timeout=5)
            self.fail(f'daemon unavailable: {health} rc={self.daemon.returncode}: {stdout.decode()}{stderr.decode()}')
        self.evidence('goal.completed', 'initial', goal_id='g1', attempt_id=1)

    def tearDown(self):
        self.daemon.terminate()
        self.daemon.communicate(timeout=5)
        self.tmp.cleanup()

    def script(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def fake_process(self, pid, argv, handle):
        path = self.proc / str(pid)
        path.mkdir()
        (path / 'cmdline').write_bytes(('\0'.join(argv) + '\0').encode())
        (path / 'environ').write_bytes(f'ORCA_TERMINAL_HANDLE={handle}\0'.encode())
        (path / 'stat').write_text(f'{pid} (test) S 1 0 0')
        (path / 'cwd').symlink_to(self.root)

    def evidence(self, kind, ident, **identity):
        code, value = self.client.evidence({'event_id': ident, 'kind': kind, 'worker': 'minecraft',
            'source': 'qoder_jsonl', 'payload': {}, **identity})
        self.assertEqual(code, 200, value)
        return value

    def tick(self, *args, env=None):
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/spectre-loop.py'),
            '--workers-file', str(self.workers), *args], capture_output=True, text=True,
            env={**self.env, **(env or {})}, timeout=15)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return json.loads(result.stdout)

    def lines(self):
        return [json.loads(line) for line in self.sent.read_text().splitlines()] if self.sent.exists() else []

    def test_disabled_and_dry_run_do_not_write_or_type(self):
        self.assertTrue(self.tick(env={'SPECTRE_LOOP': '0'})['disabled'])
        self.tick('--dry-run')
        self.assertFalse(self.state.exists())
        self.assertEqual(self.lines(), [])

    def test_planner_off_skips_minecraft(self):
        result = self.tick(env={'ASTRA_ENABLED': '0'})
        self.assertEqual(result['actions'][0]['reason'], 'planner_pin')
        self.assertEqual(self.lines(), [])

    def test_real_flow_plan_once_result_then_efficient_packet(self):
        first = self.tick()
        self.assertEqual(first['actions'][0]['action'], 'plan')
        self.assertEqual(self.client.snapshot('minecraft')['goal']['state'], 'ASSIGNING')
        ident = read_json(self.state)['workers']['minecraft']['planning']['request_id']
        self.assertEqual(self.lines()[0][self.lines()[0].index('--terminal') + 1], 'term_astra')
        self.tick()
        self.assertEqual(len(self.lines()), 1)
        write_json(self.packet_dir / f'{ident}.json', {'request_id': ident, 'worker': 'minecraft',
            'wake_reason': 'completed', 'packets': [{'id': 'p1', 'assignee': 'efficient', 'kind': 'mechanical',
            'goal': 'Rename the fixture', 'acceptance': ['Fixture tests pass'], 'requires_astra_review': False}]})
        self.tick()
        self.tick()
        self.assertEqual(len(self.lines()), 2)
        sent = self.lines()[1]
        self.assertEqual(sent[sent.index('--terminal') + 1], 'term_eff')
        self.assertTrue(sent[sent.index('--text') + 1].startswith('/goal Rename the fixture'))
        self.assertEqual(self.client.snapshot('minecraft')['goal']['state'], 'INJECTED')
        self.tick()
        self.assertEqual(len(self.lines()), 2)
        repeat = subprocess.run([str(self.bin / 'bridge'), '--dispatch', 'plan', 'minecraft',
            '--request-id', ident], env=self.env, capture_output=True, text=True)
        self.assertNotEqual(repeat.returncode, 0)
        self.assertEqual(len(self.lines()), 2)

    def test_plus_burn_refuses_without_create(self):
        (self.proc / '101/cmdline').write_bytes(b'codex\0-m\0gpt-6-astra\0-c\0model_provider=openai\0')
        result = subprocess.run([str(self.bin / 'bridge'), '--dispatch', 'plan', 'minecraft',
            '--request-id', 'r1'], capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['evt'], 'dispatch_astra_plus_burn')
        self.assertEqual(self.lines(), [])

    def test_fixture_cannot_be_used_to_write_into_live_workers(self):
        fixture = self.root / 'snapshots.json'
        write_json(fixture, {'minecraft': self.client.snapshot('minecraft')})
        result = subprocess.run([sys.executable, str(ROOT / 'scripts/spectre-loop.py'),
            '--snapshot-file', str(fixture)], capture_output=True, text=True, env=self.env)
        self.assertEqual(result.returncode, 2)
        self.assertIn('--snapshot-file requires --dry-run', result.stderr)
        self.assertEqual(self.lines(), [])


if __name__ == '__main__':
    unittest.main()
