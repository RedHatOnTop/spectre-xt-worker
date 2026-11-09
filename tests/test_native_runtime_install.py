"""Exercise installed CLI commands against a bounded fake Orca runtime."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NativeInstallationTest(unittest.TestCase):
    def test_coordinator_resume_pins_model_and_preserves_workspace_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / '.local/bin/codex'
            binary.parent.mkdir(parents=True)
            binary.write_text('''#!/usr/bin/env python3
import json,os,sys
from pathlib import Path
Path(os.environ['CAPTURE']).write_text(json.dumps(sys.argv[1:]))
''')
            binary.chmod(0o755)
            capture = root / 'argv.json'
            session = '01a196ee-87bc-76c0-a62f-b6fafbc544c9'
            env = {**os.environ, 'HOME': directory, 'CAPTURE': str(capture),
                   'ORCA_TERMINAL_HANDLE': 'term_fixture', 'ORCA_PANE_KEY': 'pane_fixture'}
            result = subprocess.run(['bash', str(ROOT / 'scripts/spectre-coordinator.sh'), 'resume', session],
                                    cwd=root, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(json.loads(capture.read_text())[:6],
                             ['--no-daemon', '--profile', 'orca-agentrouter-b',
                              '-c', 'check_for_update_on_startup=false', '-c'])
            argv = json.loads(capture.read_text())
            self.assertNotIn('codex-agentrouter-portable', argv)
            self.assertNotIn('18789', argv)
            self.assertEqual(argv[-4:], ['resume', session, '--model', 'gpt-6.1-sol'])
            self.assertEqual(len(list((root / '.local/state/remote-agent/coordinators').glob('*.lock'))), 1)

    def test_coordinator_refuses_korean_agents_without_overlay(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            binary = root / '.local/bin/codex'
            binary.parent.mkdir(parents=True)
            binary.write_text('#!/bin/sh\nexit 0\n')
            binary.chmod(0o755)
            agents = root / '.codex/AGENTS.md'
            agents.parent.mkdir(parents=True)
            agents.write_text('Reply in Korean.\n')
            session = '01a196ee-87bc-76c0-a62f-b6fafbc544c9'
            env = {**os.environ, 'HOME': directory,
                   'ORCA_TERMINAL_HANDLE': 'term_fixture', 'ORCA_PANE_KEY': 'pane_fixture'}
            result = subprocess.run(['bash', str(ROOT / 'scripts/spectre-coordinator.sh'), 'resume', session],
                                    cwd=root, env=env, capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
            self.assertIn('AGENTS overlay', result.stderr)

    def test_coordinator_resume_refuses_ambiguous_session_selector(self):
        result = subprocess.run(['bash', str(ROOT / 'scripts/spectre-coordinator.sh'), 'resume', '--last'],
                                text=True, capture_output=True, check=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Usage:', result.stderr)

    def test_coordinator_refuses_invisible_start(self):
        with tempfile.TemporaryDirectory() as d:
            prompt = Path(d) / 'prompt.md'
            prompt.write_text('Read only.')
            env = {k: v for k, v in os.environ.items() if k not in ['ORCA_TERMINAL_HANDLE', 'ORCA_PANE_KEY']}
            result = subprocess.run(['bash', str(ROOT / 'scripts/spectre-coordinator.sh'), str(prompt)],
                                    env=env, text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('registered Orca terminal', result.stderr)

    def test_legacy_installer_refuses_to_recreate_old_authority(self):
        with tempfile.TemporaryDirectory() as d:
            result = subprocess.run(['bash', str(ROOT / 'scripts/install-control-plane.sh')],
                                    env={**os.environ, 'DESTDIR': d, 'PERSON_HOME': '/home/test'},
                                    text=True, capture_output=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('retired', result.stderr.lower())
            self.assertFalse((Path(d) / 'usr/local/bin/spectre-state').exists())

    def test_installed_observer_reports_missing_worker_and_preserves_errors(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            env = {**os.environ, 'DESTDIR': d, 'PERSON_HOME': '/home/test'}
            install = subprocess.run(['bash', str(ROOT / 'scripts/install-orca-runtime.sh')],
                                     env=env, text=True, capture_output=True)
            self.assertEqual(install.returncode, 0, install.stdout + install.stderr)
            cli = root / 'fake-orca'
            cli.write_text('''#!/usr/bin/env python3
import json, sys
command = sys.argv[1:3]
data = {'runtime': {'reachable': True, 'state': 'ready'}}
if command == ['worktree', 'ps']: data = {'worktrees': []}
elif command == ['terminal', 'list']: data = {'terminals': []}
elif command == ['orchestration', 'worker-list']: data = {'workers': [], 'page': {'hasMore': False}}
elif command == ['orchestration', 'run-list']: data = {'runs': [], 'nextCursor': None}
print(json.dumps({'ok': True, 'result': data}))
''')
            cli.chmod(0o755)
            config = root / 'config.json'
            config.write_text('{"schema_version":1,"required_workspaces":["/project"]}')
            command = [str(root / 'usr/local/bin/spectre-runtime'), '--orca', str(cli), '--config', str(config)]
            missing = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(missing.returncode, 1, missing.stderr)
            self.assertIn('required_workspace_unassigned', missing.stdout)
            config.write_text('{"schema_version":1,"required_workspaces":[]}')
            idle = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(idle.returncode, 0, idle.stderr)
            self.assertEqual(json.loads(idle.stdout)['status'], 'idle')
            cli.write_text('#!/bin/sh\nprintf "offline verbatim" >&2\nexit 7\n')
            offline = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(offline.returncode, 1)
            self.assertIn('offline verbatim', offline.stdout)
            self.assertEqual(json.loads(offline.stdout)['status'], 'unverifiable')
            retirement = subprocess.run([str(root / 'usr/local/bin/spectre-retire-legacy'), '--help'],
                                        text=True, capture_output=True)
            self.assertEqual(retirement.returncode, 0, retirement.stderr)


if __name__ == '__main__':
    unittest.main()
