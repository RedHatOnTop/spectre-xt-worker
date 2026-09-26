"""spectre-offload stops the Studio it started on every exit path, and only once."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/spectre-offload.sh'


class OffloadStopTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'calls.log'
        self.log.write_text('')
        env_file = self.root / 'lightning.env'
        env_file.write_text('LIGHTNING_TEAMSPACE=team\nLIGHTNING_STUDIO=box\n'
                            'LIGHTNING_USER_ID=user\nLIGHTNING_API_KEY=placeholder\n')
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'a.txt').write_text('a\n')
        self.fake('lightning', 'echo "lightning $*" >> "$LOG"')
        self.fake('rsync', 'echo "rsync $*" >> "$LOG"')
        self.env = {**os.environ, 'PATH': f'{self.bin}:/usr/bin:/bin', 'LOG': str(self.log),
                    'HOME': str(self.root / 'home'), 'LIGHTNING_ENV_FILE': str(env_file),
                    'LIGHTNING_BIN': str(self.bin / 'lightning'), 'LIGHTNING_SSH_TIMEOUT': '5'}

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/bash\n' + body + '\n')
        path.chmod(0o755)

    def ssh(self, body):
        self.fake('ssh', 'echo "ssh $*" >> "$LOG"\n' + body)

    def offload(self, *args):
        return subprocess.run(['bash', str(SCRIPT), *args], env=self.env, capture_output=True,
                              text=True, timeout=60)

    def stops(self):
        return [line for line in self.log.read_text().splitlines()
                if line.startswith('lightning studio stop')]

    def test_a_failure_after_the_start_still_stops_the_studio(self):
        self.ssh('case "$*" in *"command -v rsync"*|*apt-get*) exit 1 ;; esac\nexit 0')
        out = self.offload('--repo', str(self.repo), '--', 'true')
        self.assertEqual(out.returncode, 6, out.stderr)
        self.assertIn('could not install rsync in box', out.stderr)
        self.assertEqual(self.stops(), ['lightning studio stop --name box --teamspace team'])

    def test_a_normal_run_stops_the_studio_once(self):
        self.ssh('exit 0')
        out = self.offload('--repo', str(self.repo), '--', 'true')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(self.stops()), 1)

    def test_keep_and_the_report_leave_the_studio_alone(self):
        self.ssh('exit 0')
        kept = self.offload('--keep', '--repo', str(self.repo), '--', 'true')
        self.assertEqual(kept.returncode, 0, kept.stderr)
        report = self.offload('--studio-report')
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertEqual(self.stops(), [])


if __name__ == '__main__':
    unittest.main()
