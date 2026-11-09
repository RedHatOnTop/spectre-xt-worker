"""The real native foreground launcher pins routes and waits for exact child exit."""
import os
from pathlib import Path
import shutil
import signal
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class NativeLaunchTest(unittest.TestCase):
    def test_direct_launcher_stop_signals_reach_worker_without_killing_its_execution(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            script = root / 'worker.py'
            script.write_text('import signal,sys,time\n'
                'def stop(number, frame):\n'
                ' print("STOP="+str(number), flush=True)\n'
                ' raise SystemExit(0)\n'
                'for number in (signal.SIGTERM,signal.SIGINT,signal.SIGHUP):\n'
                ' signal.signal(number,stop)\n'
                'print("READY",flush=True)\n'
                'while True: time.sleep(1)\n')
            binary = root / 'dsh-worker'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-DWORKER_SCRIPT="' + str(script) + '"', str(ROOT / 'scripts/dsh-worker-launch.c'),
                            '-o', str(binary)], check=True, capture_output=True)
            for number in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                with self.subTest(signal=number):
                    child = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                             text=True, start_new_session=True)
                    try:
                        self.assertEqual(child.stdout.readline().strip(), 'READY')
                        child.send_signal(number)
                        stdout, stderr = child.communicate(timeout=1)
                        self.assertEqual(child.returncode, 0, stderr)
                        self.assertIn('STOP=' + str(number), stdout)
                    finally:
                        if child.poll() is None:
                            os.killpg(child.pid, signal.SIGKILL)
                        child.communicate()

    def test_real_compiled_launcher_pins_both_routes_and_preserves_exit(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            script = root / 'worker.py'
            script.write_text('import sys\nprint(sys.argv[sys.argv.index("--route")+1])\nraise SystemExit(7)\n')
            binary = root / 'dsh-worker'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-DWORKER_SCRIPT="' + str(script) + '"', str(ROOT / 'scripts/dsh-worker-launch.c'),
                            '-o', str(binary)], check=True, capture_output=True)
            go = root / 'dsh-worker-go'
            shutil.copy2(binary, go)
            for path, route in ((binary, 'clinepass'), (go, 'opencode-go')):
                result = subprocess.run([str(path)], capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 7, result.stderr)
                self.assertEqual(result.stdout.strip(), route)
            result = subprocess.run([str(binary), '--route', 'unknown-paid'], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('pins its route', result.stderr)
            wrong = root / 'other-worker'
            shutil.copy2(binary, wrong)
            result = subprocess.run([str(wrong)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('unknown route-specific', result.stderr)

    def test_carrier_map_replaces_the_launcher_with_python_for_that_route(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            library = root / 'lib'
            library.mkdir()
            script = library / 'scripts' / 'jcode.py'
            script.parent.mkdir()
            script.write_text('print("unused")\n')
            (library / 'carrier-map').write_text(f'clinepass {script}\n')
            python = root / 'python-stub'
            source = root / 'python-stub.c'
            source.write_text('#include <stdio.h>\n#include <unistd.h>\nint main(int argc, char **argv) {\n'
                              ' for (int i = 0; i < argc; i++) puts(argv[i]);\n fflush(stdout);\n'
                              ' sleep(30);\n return 0;\n}\n')
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror', str(source), '-o', str(python)],
                           check=True, capture_output=True)
            binary = root / 'dsh-worker'
            subprocess.run(['cc', '-std=c11', '-Wall', '-Wextra', '-Werror',
                            '-DWORKER_ROOT="' + str(library) + '"',
                            '-DPYTHON="' + str(python) + '"',
                            str(ROOT / 'scripts/dsh-worker-launch.c'), '-o', str(binary)],
                           check=True, capture_output=True)
            child = subprocess.Popen([str(binary)], stdout=subprocess.PIPE, text=True)
            try:
                self.assertEqual(child.stdout.readline().strip(), 'python3')
                self.assertEqual(child.stdout.readline().strip(), str(script))
                self.assertEqual(child.stdout.readline().strip(), '--route')
                self.assertEqual(child.stdout.readline().strip(), 'clinepass')
                self.assertEqual(Path(f'/proc/{child.pid}/exe').resolve(), python.resolve())
            finally:
                child.kill()
                child.communicate()
            (library / 'carrier-map').write_text('opencode-go /tmp/missing.py\n')
            result = subprocess.run([str(binary)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 127)
            self.assertIn('no script', result.stderr)
