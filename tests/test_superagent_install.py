"""Reject incomplete runtime sources before modifying an installed continuity tree."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class SuperagentInstallTest(unittest.TestCase):
    def test_missing_runtime_dependency_fails_before_existing_files_change(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, destination = base / 'source', base / 'destination'
            scripts = source / 'scripts'
            scripts.mkdir(parents=True)
            shutil.copy2(ROOT / 'scripts/install-superagent.sh', scripts / 'install-superagent.sh')
            shutil.copytree(ROOT / 'scripts/superagent_queue', scripts / 'superagent_queue')
            shutil.copytree(ROOT / 'scripts/orca_runtime', scripts / 'orca_runtime')
            (scripts / 'superagent_queue/offload_source.py').unlink()
            installed = destination / 'usr/local/lib/spectre-superagent/superagent_queue/driver.py'
            installed.parent.mkdir(parents=True)
            installed.write_text('Original installed runtime\n')
            result = subprocess.run(['bash', str(scripts / 'install-superagent.sh')],
                env={**os.environ, 'DESTDIR': str(destination), 'PERSON_HOME': '/home/fixture'},
                text=True, capture_output=True, timeout=10, check=False)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("cannot import name 'offload_source'", result.stderr)
            self.assertEqual(installed.read_text(), 'Original installed runtime\n')
            self.assertFalse((installed.parent / 'native.py').exists())


if __name__ == '__main__':
    unittest.main()
