"""burst-offload terminates the instance it started on every exit path, and hands back the
command's exit status with one JSON line."""
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import unittest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/burst-offload.sh'
UP = ('{"id":"i-0abc","ip":"203.0.113.5","user":"ec2-user","type":"c7i-flex.large",'
      '"az":"us-west-2a","region":"us-west-2","ttl_min":90}')


class BurstOffloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.log = self.root / 'calls.log'
        self.log.write_text('')
        self.repo = self.root / 'repo'
        self.repo.mkdir()
        (self.repo / 'a.txt').write_text('a\n')
        self.fake('aws-burst', 'echo "aws-burst $*" >> "$LOG"\n'
                               'case "$1" in\n'
                               f"  up) echo '{UP}' ;;\n"
                               '  sshopts) echo "-i /dev/null" ;;\n'
                               'esac')
        self.fake('aws', 'exit 0')
        self.fake('rsync', 'echo "rsync $*" >> "$LOG"')
        self.env = {**os.environ, 'PATH': f'{self.bin}:/usr/bin:/bin', 'LOG': str(self.log),
                    'HOME': str(self.root / 'home'),
                    'BURST_OFFLOAD_HOME': str(self.root / 'runs')}

    def tearDown(self):
        self.tmp.cleanup()

    def fake(self, name, body):
        path = self.bin / name
        path.write_text('#!/bin/bash\n' + body + '\n')
        path.chmod(0o755)

    def ssh(self, prepare='exit 0', run='exit 0'):
        self.fake('ssh', 'echo "ssh $*" >> "$LOG"\n'
                         'case "$*" in\n'
                         f'  *"dnf install"*) {prepare} ;;\n'
                         f'  *"bash -s"*) cat > "$LOG.script"; {run} ;;\n'
                         'esac\n'
                         'exit 0')

    def offload(self, *args):
        return subprocess.run(['bash', str(SCRIPT), *args], env=self.env, capture_output=True,
                              text=True, timeout=60)

    def downs(self):
        return [line for line in self.log.read_text().splitlines()
                if line.startswith('aws-burst down')]

    def test_a_normal_run_terminates_the_instance_once(self):
        self.ssh()
        out = self.offload('--repo', str(self.repo), '--', 'make', 'test')
        self.assertEqual(out.returncode, 0, out.stderr)
        result = json.loads(out.stdout)
        self.assertTrue(result['ok'])
        self.assertEqual(result['instance'], 'i-0abc')
        self.assertEqual(self.downs(), ['aws-burst down i-0abc'])
        self.assertIn('make test', Path(f'{self.log}.script').read_text())

    def test_the_commands_exit_status_is_the_wrappers(self):
        self.ssh(run='exit 3')
        out = self.offload('--repo', str(self.repo), '--', 'false')
        self.assertEqual(out.returncode, 3, out.stderr)
        result = json.loads(out.stdout)
        self.assertEqual((result['ok'], result['exit']), (False, 3))
        self.assertEqual(len(self.downs()), 1)

    def test_a_failed_preparation_still_terminates_the_instance(self):
        self.ssh(prepare='exit 1')
        out = self.offload('--repo', str(self.repo), '--', 'true')
        self.assertEqual(out.returncode, 5, out.stderr)
        self.assertIn('preparing i-0abc failed', out.stderr)
        self.assertEqual(out.stdout, '')
        self.assertEqual(self.downs(), ['aws-burst down i-0abc'])

    def test_no_instance_means_nothing_to_terminate(self):
        self.fake('aws-burst', 'echo "aws-burst $*" >> "$LOG"; exit 1')
        out = self.offload('--repo', str(self.repo), '--', 'true')
        self.assertEqual(out.returncode, 3, out.stderr)
        self.assertEqual(self.downs(), [])

    def test_a_setup_command_runs_before_the_command_and_can_fail_the_run(self):
        self.ssh()
        out = self.offload('--repo', str(self.repo), '--setup', 'make deps', '--', 'make', 'test')
        self.assertEqual(out.returncode, 0, out.stderr)
        script = Path(f'{self.log}.script').read_text()
        self.assertLess(script.index('make deps'), script.index('make test'))
        self.assertIn('setup failed', script)

    def test_rust_is_installed_only_when_asked(self):
        self.ssh()
        self.offload('--repo', str(self.repo), '--', 'true')
        self.assertNotIn('rustup', self.log.read_text())
        self.offload('--repo', str(self.repo), '--rust', '1.97.1', '--', 'true')
        self.assertIn('--default-toolchain 1.97.1', self.log.read_text())

    def test_artifacts_come_back_into_the_run_directory(self):
        self.ssh()
        out = self.offload('--repo', str(self.repo), '--artifact', 'out/report.txt', '--', 'true')
        result = json.loads(out.stdout)
        self.assertEqual(result['pulled'], ['out/report.txt'])
        self.assertIn(f'ec2-user@203.0.113.5:work/out/report.txt {result["artifacts"]}/out/report.txt',
                      self.log.read_text())

    def test_a_terminated_run_exits_at_once_and_terminates_the_instance(self):
        self.ssh(run='sleep 30')
        proc = subprocess.Popen(['bash', str(SCRIPT), '--repo', str(self.repo), '--', 'true'],
                                env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)
        deadline = time.monotonic() + 15
        while 'bash -s' not in self.log.read_text() and time.monotonic() < deadline:
            time.sleep(0.1)
        os.killpg(proc.pid, signal.SIGTERM)
        stdout, stderr = proc.communicate(timeout=30)
        self.assertEqual(proc.returncode, 143, stderr)
        self.assertEqual(stdout, '')
        self.assertEqual(self.downs(), ['aws-burst down i-0abc'])

    def test_keep_and_dry_run_leave_instances_alone(self):
        self.ssh()
        kept = self.offload('--keep', '--repo', str(self.repo), '--', 'true')
        self.assertEqual(kept.returncode, 0, kept.stderr)
        self.assertIn('aws-burst down i-0abc', kept.stderr)
        dry = self.offload('--dry-run', '--repo', str(self.repo), '--', 'true')
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertTrue(json.loads(dry.stdout)['dry_run'])
        self.assertEqual(self.downs(), [])
        self.assertEqual(self.log.read_text().count('aws-burst up'), 1)


if __name__ == '__main__':
    unittest.main()
