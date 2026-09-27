"""spectre-offload releases its Studio lease on every exit path and stops only a Studio
that a lease started, with the last lease."""
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
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
        self.fake('lightning', 'echo "lightning $*" >> "$LOG"\n'
                               'if [ "$2" = list ]; then\n'
                               '  printf \'[{"name": "box", "status": "%s"}]\' "${STATUS:-Stopped}"\n'
                               'fi')
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
        self.fake('ssh', 'echo "ssh $*" >> "$LOG"\n'
                         'if [ "${!#}" = "echo spectre-ready" ]; then echo spectre-ready; exit 0; fi\n'
                         + body)

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

    def leases(self):
        return self.root / 'home/.local/state/remote-agent/offload-leases'

    def test_a_live_offload_elsewhere_keeps_the_studio_running(self):
        self.ssh('exit 0')
        self.leases().mkdir(parents=True)
        other = self.leases() / str(os.getpid())
        other.write_text(f'{os.getpid()}\n')
        out = self.offload('--repo', str(self.repo), '--', 'true')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertIn('leaving Studio box running for 1 other offload(s)', out.stderr)
        self.assertEqual(self.stops(), [])
        self.assertEqual(sorted(path.name for path in self.leases().iterdir()),
                         ['.lock', '.started', str(os.getpid())])

    def test_a_dead_offloads_lease_is_dropped_and_the_studio_stops(self):
        self.ssh('exit 0')
        gone = subprocess.Popen(['true'])
        gone.wait()
        self.leases().mkdir(parents=True)
        (self.leases() / str(gone.pid)).write_text(f'{gone.pid}\n')
        out = self.offload('--repo', str(self.repo), '--', 'true')
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(len(self.stops()), 1)
        self.assertEqual([path.name for path in self.leases().iterdir()], ['.lock'])

    def test_a_studio_someone_else_started_is_left_running(self):
        self.ssh('exit 0')
        for status in ('Running', 'Pending'):
            with self.subTest(status=status):
                self.env['STATUS'] = status
                out = self.offload('--repo', str(self.repo), '--', 'true')
                self.assertEqual(out.returncode, 0, out.stderr)
                self.assertIn('it was already up when this offload began', out.stderr)
                self.assertEqual(self.stops(), [])

    def test_readiness_waits_out_the_setup_message(self):
        self.env['LIGHTNING_SSH_TIMEOUT'] = '30'
        seen = self.root / 'setup-seen'
        self.fake('ssh', 'echo "ssh $*" >> "$LOG"\n'
                         f'if [ "${{!#}}" = "echo spectre-ready" ] && [ ! -e {seen} ]; then\n'
                         f'  touch {seen}; echo "Error: We are still setting things up for you"; exit 0\n'
                         'fi\n'
                         'if [ "${!#}" = "echo spectre-ready" ]; then echo spectre-ready; fi\n'
                         'exit 0')
        out = self.offload('--repo', str(self.repo), '--', 'true')
        self.assertEqual(out.returncode, 0, out.stderr)
        probes = [line for line in self.log.read_text().splitlines() if 'spectre-ready' in line]
        self.assertEqual(len(probes), 2)
        self.assertEqual(len(self.stops()), 1)

    def test_a_terminated_run_exits_at_once_and_releases_the_studio(self):
        self.ssh('case "${!#}" in *"cd "*) sleep 30 ;; esac\nexit 0')
        proc = subprocess.Popen(['bash', str(SCRIPT), '--repo', str(self.repo), '--', 'true'],
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        deadline = time.monotonic() + 15
        while ' && ' not in self.log.read_text() and time.monotonic() < deadline:
            time.sleep(0.1)
        os.killpg(proc.pid, signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 143, stderr)
        self.assertEqual(stdout, '')
        self.assertNotIn('du -sb', self.log.read_text())
        self.assertEqual(len(self.stops()), 1)
        self.assertEqual([path.name for path in self.leases().iterdir()], ['.lock'])

    def test_keep_and_the_report_leave_the_studio_alone(self):
        self.ssh('exit 0')
        kept = self.offload('--keep', '--repo', str(self.repo), '--', 'true')
        self.assertEqual(kept.returncode, 0, kept.stderr)
        report = self.offload('--studio-report')
        self.assertEqual(report.returncode, 0, report.stderr)
        self.assertEqual(self.stops(), [])


if __name__ == '__main__':
    unittest.main()
