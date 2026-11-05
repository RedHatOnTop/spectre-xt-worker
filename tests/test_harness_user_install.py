"""Install bounded harness policy without replacing sessions or existing instructions."""
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('harness_install', ROOT / 'scripts/install-harness-workers.py')
INSTALL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(INSTALL)


class HarnessUserInstallTest(unittest.TestCase):
    def test_user_install_preserves_existing_global_rules_and_replays_idempotently(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            memory = home / '.codex/AGENTS.md'
            memory.parent.mkdir()
            original = '# Existing rules\n\nDo not change production.\n'
            memory.write_text(original)
            first = INSTALL.install(home, ROOT)
            second = INSTALL.install(home, ROOT)
            self.assertEqual(first['runtime_sha256'], second['runtime_sha256'])
            self.assertTrue((home / '.local/bin/harness-workers').exists())
            self.assertTrue(memory.read_text().startswith(original))
            self.assertEqual(memory.read_text().count('<!-- harness-workers:begin -->'), 1)
            self.assertEqual(Path(first['memory_backup']).read_text(), original)
            policy = json.loads((home / '.config/remote-agent/harness-workers.json').read_text())
            self.assertEqual(policy['max_workers'], 2)
            self.assertEqual(policy['agents'], ['dsh-worker', 'dsh-worker-go'])

    def test_existing_different_policy_or_nonowned_launch_path_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            policy = home / '.config/remote-agent/harness-workers.json'
            policy.parent.mkdir(parents=True)
            policy.write_text('{"existing":"different policy"}')
            policy.chmod(0o600)
            with self.assertRaisesRegex(ValueError, 'policy'):
                INSTALL.install(home, ROOT)
            self.assertEqual(policy.read_text(), '{"existing":"different policy"}')
            self.assertFalse((home / '.local/bin/harness-workers').exists())

    def test_broken_runtime_source_fails_before_policy_or_global_rules_change(self):
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, home = base / 'source', base / 'home'
            home.mkdir()
            source.mkdir()
            shutil.copytree(ROOT / 'scripts', source / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
            shutil.copytree(ROOT / 'config', source / 'config')
            (source / 'scripts/superagent_queue/worker_launch.py').unlink()
            with self.assertRaisesRegex(RuntimeError, 'worker_launch'):
                INSTALL.install(home, source)
            self.assertFalse((home / '.config/remote-agent/harness-workers.json').exists())
            self.assertFalse((home / '.codex/AGENTS.md').exists())

    def test_reused_runtime_is_hash_verified_not_trusted_by_directory_name(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            report = INSTALL.install(home, ROOT)
            module = Path(report['runtime']) / 'lib/spectre-superagent/superagent_queue/worker_pool.py'
            module.write_text('Changed installed runtime')
            with self.assertRaisesRegex(ValueError, 'runtime'):
                INSTALL.install(home, ROOT)

    def test_symlinked_global_memory_is_not_modified_or_followed(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / 'home'
            home.mkdir()
            target = Path(directory) / 'outside.md'
            target.write_text('Do not modify outside home')
            memory = home / '.codex/AGENTS.md'
            memory.parent.mkdir()
            memory.symlink_to(target)
            with self.assertRaisesRegex(ValueError, 'symlink'):
                INSTALL.install(home, ROOT)
            self.assertEqual(target.read_text(), 'Do not modify outside home')
            self.assertFalse((home / '.config/remote-agent/harness-workers.json').exists())

    def test_existing_worker_library_follows_the_installed_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            stable = home / '.local/lib/spectre-superagent'
            stable.mkdir(parents=True)
            report = INSTALL.install(home, ROOT)
            link = stable / 'lib/spectre-superagent'
            self.assertTrue(link.is_symlink())
            self.assertEqual(link.resolve(), Path(report['runtime']) / 'lib/spectre-superagent')
            self.assertFalse(report['session_restart'])
            self.assertFalse(report['native_start'])

    def test_malformed_memory_markers_fail_before_any_install_changes(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            memory = home / '.codex/AGENTS.md'
            memory.parent.mkdir()
            memory.write_text('<!-- harness-workers:begin -->\nIncomplete managed block')
            with self.assertRaisesRegex(ValueError, 'markers'):
                INSTALL.install(home, ROOT)
            self.assertFalse((home / '.config/remote-agent/harness-workers.json').exists())


if __name__ == '__main__':
    unittest.main()
