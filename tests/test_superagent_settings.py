"""Operator expectations and operational locks reject ambiguous ownership."""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import driver, settings

TARGET = {'run_id': 'run_1', 'terminal_handle': 'term_1', 'worktree_id': 'repo::/work',
          'incarnation_id': 'process_1', 'text': 'Continue approved work.'}


class SettingsTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path = self.root / 'settings.json'

    def write(self, value):
        self.path.write_text(json.dumps(value))
        self.path.chmod(0o600)

    def test_approved_native_config_is_read_without_mutation(self):
        self.write({'schema_version': 1, 'coordinators': [TARGET]})
        before = self.path.read_bytes()
        self.assertEqual(settings.config(self.path)['coordinators'], [TARGET])
        self.assertEqual(self.path.read_bytes(), before)

    def test_unknown_and_duplicate_config_are_not_accepted(self):
        for value in ({}, [], {'schema_version': 1, 'coordinators': {}},
                      {'schema_version': 1, 'coordinators': [TARGET, TARGET]}):
            self.write(value)
            with self.subTest(value=value), self.assertRaises(ValueError):
                settings.config(self.path)

    def test_symlink_and_nonprivate_settings_fail(self):
        self.write({})
        link = self.root / 'link.json'
        link.symlink_to(self.path)
        with self.assertRaisesRegex(ValueError, 'symlinks'):
            settings.read_private(link)
        self.path.chmod(0o644)
        with self.assertRaisesRegex(ValueError, 'private'):
            settings.read_private(self.path)

    def test_missing_disposition_is_not_a_revocation(self):
        self.assertEqual(settings.dispositions(self.path), {})

    def test_structured_dispositions_are_read(self):
        value = {'run_1': {'state': 'waiting_approval', 'gate_ids': ['gate_1']}}
        self.write(value)
        self.assertEqual(settings.dispositions(self.path), value)

    def test_lock_prevents_overlapping_ticks_and_can_reopen(self):
        lock = self.root / 'private' / 'tick.lock'
        with settings.lock(lock):
            with self.assertRaises(BlockingIOError):
                with settings.lock(lock):
                    self.fail('Second tick unexpectedly acquired the operational lock')
        with settings.lock(lock):
            self.assertEqual(lock.stat().st_mode & 0o777, 0o600)

    def test_lock_rejects_nonprivate_parent_and_file(self):
        lock = self.root / 'unsafe.lock'
        lock.touch(mode=0o644)
        with self.assertRaisesRegex(ValueError, 'private'):
            with settings.lock(lock):
                self.fail('Unsafe lock accepted')

    def test_job_wait_requires_explicit_job_identifiers(self):
        with self.assertRaises(ValueError):
            driver.validate_dispositions({'run_1': {'state': 'waiting_job', 'job_ids': []}})
        value = {'run_1': {'state': 'waiting_job', 'job_ids': ['job_1']}}
        self.assertEqual(driver.validate_dispositions(value), value)

    def test_offload_fetch_binding_names_exact_existing_studio_and_approved_run(self):
        value = {'schema_version': 1, 'coordinators': [TARGET],
                 'offload': [{'run_id': 'run_1', 'studio': 'existing-studio'}]}
        self.write(value)
        self.assertEqual(settings.config(self.path), value)
        for row in ({'run_id': 'other', 'studio': 'studio'}, {'run_id': 'run_1', 'studio': 'user@remote'},
                    {'run_id': 'run_1', 'studio': '-oProxyCommand=bad'}, {'run_id': 'run_1'},
                    {'run_id': 'run_1', 'studio': 'studio', 'funding': 'new'}):
            self.write({**value, 'offload': [row]})
            with self.subTest(row=row), self.assertRaises(ValueError):
                settings.config(self.path)
        self.write({**value, 'offload': [value['offload'][0], value['offload'][0]]})
        with self.assertRaises(ValueError):
            settings.config(self.path)


if __name__ == '__main__':
    unittest.main()
