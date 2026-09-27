from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / 'scripts/spectre-omni-proxy'


class OmniWrapperTest(unittest.TestCase):
    def test_reads_private_keys_and_executes_go_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / 'config.json'
            config.write_text('{\n  "providers": []\n}\n')
            config.chmod(0o600)
            client = root / 'client_key'
            client.write_text('client-secret\n')
            client.chmod(0o600)
            paid = root / 'paid_key'
            paid.write_text('paid-secret\n')
            paid.chmod(0o600)
            binary = root / 'go-binary'
            binary.write_text('#!' + sys.executable + '\n'
                'import os,sys\n'
                'assert os.environ["OMNI_CLIENT_KEY"] == "client-secret"\n'
                'assert os.environ["CLINE_PASS_API_KEY"] == "paid-secret"\n'
                'assert sys.argv[1:] == ["serve", "-config", ' + repr(str(config)) + ']\n')
            binary.chmod(0o755)
            env = {**os.environ, 'SPECTRE_OMNI_BIN': str(binary),
                   'SPECTRE_OMNI_CONFIG': str(config),
                   'SPECTRE_OMNI_CLIENT_KEY': str(client),
                   'SPECTRE_DSH_KEY': str(paid)}
            proc = subprocess.run([sys.executable, str(WRAPPER)], env=env,
                                  capture_output=True, text=True, timeout=5)
            self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_refuses_public_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = root / 'key'
            key.write_text('secret')
            key.chmod(0o644)
            env = {**os.environ, 'SPECTRE_OMNI_CLIENT_KEY': str(key)}
            proc = subprocess.run([sys.executable, str(WRAPPER)], env=env,
                                  capture_output=True, text=True, timeout=5)
            self.assertEqual(proc.returncode, 2)
            self.assertNotIn('secret', proc.stderr)


if __name__ == '__main__':
    unittest.main()
