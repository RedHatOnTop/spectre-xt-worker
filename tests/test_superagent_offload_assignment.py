"""Offload argv must be authorized by the exact current native Task and pane."""
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import offload_assignment
from test_superagent_assignment import CALLER, DISPATCH, TERMINAL

SPEC = {'schema_version': 1, 'command': ['printf', 'verified output'], 'artifacts': ['result.json']}
CONFIG = {'schema_version': 1, 'coordinators': [{'run_id': 'run_1', 'terminal_handle': 'coordinator_1'}],
          'offload': [{'run_id': 'run_1', 'studio': 'existing-studio'}]}


class OffloadAssignmentTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.packet = Path(self.directory.name) / 'packet.json'
        self.packet.write_text(json.dumps(SPEC))
        self.packet.chmod(0o600)
        self.preamble = Path(self.directory.name) / 'preamble.txt'
        self.preamble.write_text(self.preview())
        self.preamble.chmod(0o600)

    def preview(self):
        return 'Exact native worker instructions.\n=== TASK ===\n' + self.packet.read_text()

    def query(self, cli, args):
        if args[:2] == ['orchestration', 'dispatch-show']:
            return {'dispatch': DISPATCH, 'preamble': self.preview()}
        if args[:2] == ['orchestration', 'run-show']:
            return {'run': {'id': 'run_1', 'coordinator_handle': 'coordinator_1', 'legacy': 0}}
        if args[:2] == ['terminal', 'show']:
            return {'terminal': TERMINAL}
        if args[:2] == ['worktree', 'ps']:
            return {'worktrees': [{'worktreeId': 'repo::/work', 'path': '/work'}]}
        raise AssertionError(args)

    def prepare(self, caller=CALLER, config=CONFIG):
        return offload_assignment.prepare('/exact/orca', 'task_1', 'dispatch_1', self.packet,
                                         self.preamble, config, caller, cwd='/work')

    def test_exact_native_scope_and_original_argv_are_preserved(self):
        with patch('orca_runtime.observe.query', side_effect=self.query):
            binding, spec, studio = self.prepare()
        self.assertEqual(binding['route'], 'lightning')
        self.assertEqual(binding['packet_sha256'], hashlib.sha256(self.packet.read_bytes()).hexdigest())
        self.assertEqual(spec, SPEC)
        self.assertEqual(studio, 'existing-studio')
        self.assertEqual(binding['dispatch_id'], 'dispatch_1')

    def test_native_current_capability_must_match_original_received_preamble(self):
        capability = 'current-capability'
        current = {**DISPATCH, 'capability_hash': hashlib.sha256(capability.encode()).hexdigest()}

        def query(cli, args):
            row = self.query(cli, args)
            return {**row, 'dispatch': current} if 'dispatch' in row else row

        with patch('orca_runtime.observe.query', side_effect=query), self.assertRaisesRegex(ValueError, 'capability'):
            self.prepare()
        self.preamble.write_text(self.preview().replace('instructions.', 'instructions. --dispatch-capability ' + capability))
        with patch('orca_runtime.observe.query', side_effect=query):
            self.assertEqual(self.prepare()[0]['task_id'], 'task_1')

    def test_unapproved_run_and_missing_existing_studio_binding_never_authorize(self):
        for config in ({**CONFIG, 'offload': []}, {**CONFIG, 'coordinators': []},
                       {**CONFIG, 'offload': [{'run_id': 'another', 'studio': 'existing-studio'}]}):
            with self.subTest(config=config), patch('orca_runtime.observe.query', side_effect=self.query), \
                    self.assertRaises(ValueError):
                self.prepare(config=config)

    def test_shell_or_old_native_identity_cannot_become_an_offload_execution(self):
        with patch('orca_runtime.observe.query') as query, self.assertRaises(ValueError):
            self.prepare(caller={})
        query.assert_not_called()
        for change in ({'status': 'completed'}, {'id': 'old-dispatch'}, {'capability_revoked_at': 'revoked'}):
            def query(cli, args):
                row = self.query(cli, args)
                return {**row, 'dispatch': {**DISPATCH, **change}} if 'dispatch' in row else row
            with self.subTest(change=change), patch('orca_runtime.observe.query', side_effect=query), self.assertRaises(ValueError):
                self.prepare()

    def test_packet_digest_and_new_native_task_cannot_expand_original_scope(self):
        self.preamble.write_text(self.preview() + ' extra work')
        with patch('orca_runtime.observe.query', side_effect=self.query), self.assertRaisesRegex(ValueError, 'preamble'):
            self.prepare()

    def test_job_packet_rejects_shell_strings_extra_permissions_and_unsafe_artifacts(self):
        for change in ({'command': 'sh -c true'}, {'command': []}, {'command': ['']}, {'command': ['a\x00b']},
                       {'schema_version': True}, {'artifacts': ['../outside']}, {'artifacts': ['/absolute']},
                       {'artifacts': ['result.json', 'result.json']}, {'funding': 'new'}, {'setup': 'install paid service'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                offload_assignment.spec({**SPEC, **change})

    def test_pre_execution_confirmation_requires_identical_packet_and_workspace(self):
        with patch('orca_runtime.observe.query', side_effect=self.query):
            binding, _, _ = self.prepare()
            self.assertEqual(offload_assignment.confirm('/exact/orca', binding, self.packet, self.preamble,
                                                        CONFIG, CALLER, cwd='/work'), 'existing-studio')
        with patch('orca_runtime.observe.query', side_effect=self.query), self.assertRaisesRegex(ValueError, 'binding'):
            offload_assignment.confirm('/exact/orca', {**binding, 'packet_sha256': 'b' * 64}, self.packet,
                                       self.preamble, CONFIG, CALLER, cwd='/work')

    def test_invalid_studio_and_changed_run_coordinator_never_authorize(self):
        config = {**CONFIG, 'offload': [{'run_id': 'run_1', 'studio': 'user@foreign'}]}
        with patch('orca_runtime.observe.query', side_effect=self.query), self.assertRaisesRegex(ValueError, 'Studio'):
            self.prepare(config=config)
        changed = {**CONFIG, 'coordinators': [{'run_id': 'run_1', 'terminal_handle': 'other-coordinator'}]}
        with patch('orca_runtime.observe.query', side_effect=self.query), self.assertRaisesRegex(ValueError, 'coordinator'):
            self.prepare(config=changed)

    def test_packet_replacement_between_native_validation_and_argv_read_is_refused(self):
        original = self.packet.read_text()
        original_preview = self.preview()

        def query(cli, args):
            row = self.query(cli, args)
            if args[:2] == ['orchestration', 'dispatch-show']:
                return {**row, 'preamble': original_preview}
            if args[:2] == ['worktree', 'ps']:
                self.packet.write_text(json.dumps({**SPEC, 'command': ['sh', '-c', 'unapproved command']}))
            return row

        with patch('orca_runtime.observe.query', side_effect=query), self.assertRaisesRegex(ValueError, 'packet changed'):
            self.prepare()
        self.assertNotEqual(self.packet.read_text(), original)


if __name__ == '__main__':
    unittest.main()
