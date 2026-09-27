from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from control_plane import omni_setup


class OmniSetupTest(unittest.TestCase):
    def credentials(self, path: Path) -> None:
        path.write_text(json.dumps({'providers': {'cline': {'settings': {'auth': {
            'accessToken': 'synthetic-access', 'refreshToken': 'synthetic-refresh',
            'expiresAt': 1_800_000_000_000}}}}}), encoding='utf-8')
        path.chmod(0o600)

    def test_prepare_writes_private_go_and_dsh_configs(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            credentials = home / 'providers.json'
            self.credentials(credentials)
            paid = home / 'paid_key'
            paid.write_text('paid-secret', encoding='utf-8')
            paid.chmod(0o600)
            profiles = home / '.local/share/fullmoon-dsh/profiles'
            profiles.mkdir(parents=True)
            output = omni_setup.prepare(home, credentials, paid, 'client-secret')
            config = output['config']
            self.assertEqual(config['providers'][0]['credentialsFile'], str(credentials))
            self.assertEqual(config['routing']['fallbacks'][
                'cline-free/cline-free/deepseek-v4.1-flash'],
                ['cline-paid/cline-pass/deepseek-v4.1-flash'])
            self.assertEqual(config['apiKeyEnv'], 'OMNI_CLIENT_KEY')
            self.assertTrue({401, 403, 429}.issubset(set(config['routing']['retryStatuses'])))
            self.assertEqual((home / '.config/omni-proxy/config.json').stat().st_mode & 0o777, 0o600)
            self.assertEqual((home / '.config/omni-proxy/client_key').stat().st_mode & 0o777, 0o600)
            profile = home / '.local/share/fullmoon-dsh-free/settings.yaml'
            self.assertEqual(profile.stat().st_mode & 0o777, 0o600)
            self.assertIn('cline-free/cline-free/deepseek-v4.1-flash', profile.read_text())
            self.assertTrue((profile.parent / 'profiles').is_symlink())

    def test_prepare_refuses_non_private_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            credentials = home / 'providers.json'
            credentials.write_text('{}', encoding='utf-8')
            credentials.chmod(0o644)
            paid = home / 'paid_key'
            paid.write_text('secret', encoding='utf-8')
            paid.chmod(0o600)
            with self.assertRaisesRegex(ValueError, 'private'):
                omni_setup.prepare(home, credentials, paid, 'client-secret')

    def test_kimi_config_preserves_settings_and_targets_go_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'config.toml'
            path.write_text('default_model = "cline/kimi-k3"\n'
                '[providers.cline]\n'
                'type = "openai"\n'
                'base_url = "http://127.0.0.1:8787/v1"\n'
                'api_key = "old-key"\n'
                '[models."cline/kimi-k3"]\n'
                'provider = "cline"\n'
                'model = "cline-free/kimi-k3"\n')
            omni_setup.configure_kimi(path, 'new-key')
            text = path.read_text()
            self.assertIn('base_url = "http://127.0.0.1:8790/v1"', text)
            self.assertIn('api_key = "new-key"', text)
            self.assertIn('model = "cline-free/kimi-k3"', text)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_conflicting_free_profile_link_does_not_write_partial_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            credentials = home / 'providers.json'
            self.credentials(credentials)
            paid = home / 'paid_key'
            paid.write_text('paid-secret')
            paid.chmod(0o600)
            (home / '.local/share/fullmoon-dsh/profiles').mkdir(parents=True)
            free = home / '.local/share/fullmoon-dsh-free'
            free.mkdir(parents=True)
            (free / 'profiles').mkdir()
            with self.assertRaisesRegex(ValueError, 'already exists'):
                omni_setup.prepare(home, credentials, paid, 'client-secret')
            self.assertFalse((home / '.config/omni-proxy/config.json').exists())

    def test_invalid_oauth_snapshot_is_refused_before_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            credentials = home / 'providers.json'
            credentials.write_text('{}')
            credentials.chmod(0o600)
            paid = home / 'paid_key'
            paid.write_text('paid-secret')
            paid.chmod(0o600)
            (home / '.local/share/fullmoon-dsh/profiles').mkdir(parents=True)
            with self.assertRaisesRegex(ValueError, 'Cline OAuth'):
                omni_setup.prepare(home, credentials, paid, 'client-secret')
            self.assertFalse((home / '.config/omni-proxy/config.json').exists())

    def test_cli_invalid_kimi_config_does_not_prepare_partial_proxy(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            credentials = home / 'providers.json'
            self.credentials(credentials)
            paid = home / 'paid_key'
            paid.write_text('paid-secret')
            paid.chmod(0o600)
            (home / '.local/share/fullmoon-dsh/profiles').mkdir(parents=True)
            kimi_config = home / 'config.toml'
            kimi_config.write_text('not valid TOML =')
            script = Path(__file__).resolve().parents[1] / 'scripts/spectre-omni-configure'
            proc = subprocess.run([str(script), '--credentials-file', str(credentials),
                                   '--paid-key', str(paid), '--kimi-config', str(kimi_config)],
                                  env={**os.environ, 'HOME': str(home)},
                                  capture_output=True, text=True, timeout=5)
            self.assertEqual(proc.returncode, 1)
            self.assertFalse((home / '.config/omni-proxy/config.json').exists())


if __name__ == '__main__':
    unittest.main()
