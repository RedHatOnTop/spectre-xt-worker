"""Staged installation must run without imports from the source checkout."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstallTest(unittest.TestCase):
    def test_staged_entry_points_load_the_installed_packages(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = {**os.environ, 'DESTDIR': tmp, 'PERSON_HOME': '/home/operator',
                   'HOME': str(root / 'home/operator'), 'SPECTRE_LOOP': '0', 'PYTHONPATH': ''}
            result = subprocess.run(['bash', str(ROOT / 'scripts/install-control-plane.sh')],
                                    env=env, cwd=tmp, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            binaries = root / 'usr/local/bin'
            for command in ('spectre-loop', 'spectre-state', 'spectre-astra', 'spectre-reaper',
                            'spectre-pin-sync', 'spectre-codex-provider-health', 'dsh-clinepass',
                            'mimo-clinepass'):
                help_ = subprocess.run([str(binaries / command), '--help'], env=env,
                                       cwd=tmp, capture_output=True, text=True)
                self.assertEqual(help_.returncode, 0, command + ': ' + help_.stderr)
            disabled = subprocess.run([str(binaries / 'spectre-loop')], env=env, cwd=tmp,
                                      capture_output=True, text=True)
            self.assertEqual(disabled.returncode, 0, disabled.stdout + disabled.stderr)
            self.assertTrue(json.loads(disabled.stdout)['disabled'])
            self.assertEqual((binaries / 'dsh-clinepass').stat().st_mode & 0o777, 0o755)
            self.assertFalse((root / 'home/operator/.config/systemd/user/timers.target.wants').exists())
            check = subprocess.run(['node', '--check', str(binaries / 'spectre-slack-bridge')],
                                   capture_output=True, text=True)
            self.assertEqual(check.returncode, 0, check.stderr)
