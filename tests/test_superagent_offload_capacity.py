"""Release only verified completed-job capacity in the existing shared lease directory."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from superagent_queue import execution, offload_receipt
from test_superagent_offload_receipt import BINDING, COMMAND, remote_result


class OffloadCapacityTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.base = Path(self.directory.name) / 'private'
        self.root = self.base / 'offloads'
        self.record = offload_receipt.run(self.root, 'job_1', BINDING, COMMAND, ['/bin/true'])
        self.job = self.root / 'job_1'
        execution.persist(self.job, 'command-result.json', {**remote_result(),
            'runtime': {**remote_result()['runtime'], 'boot_id': 'remote-original-boot'}})
        offload_receipt.reconcile(self.root, 'job_1')
        self.leases = self.base / 'leases'
        self.leases.mkdir(mode=0o700)
        (self.leases / '.lock').touch(mode=0o600)
        self.reservation = self.leases / '.unverifiable-job_1'
        self.reservation.write_text('~/offload/job_1\n')
        self.reservation.chmod(0o600)
        self.cleanup = {'request_sha256': 'a' * 64, 'state': 'completed', 'tree_removed': True,
                        'studio_stopped': False, 'finished_at': 101, 'schema_version': 1}
        self.cleanup_record = {'intent_sha256': 'b' * 64, 'receipt': self.cleanup}
        (self.job / 'cleanup').mkdir(mode=0o700)
        execution.persist(self.job / 'cleanup', 'exit.json', self.cleanup_record)
        self.target = {'studio': 'existing-studio', 'teamspace': 'approved/teamspace',
                       'remote_dir': 'offload/job_1', 'remote_boot_id': 'remote-original-boot'}
        execution.persist(self.job, 'transport.json', {'schema_version': 1, 'job_id': 'job_1',
            'command_sha256': self.record['command_sha256'], 'studio': 'existing-studio',
            'remote_dir': 'offload/job_1', 'recorded_at': self.record['created_at']})

    def module(self):
        from superagent_queue import offload_capacity
        return offload_capacity

    def capacity(self, owned=False):
        module = self.module()
        marker = None
        if owned:
            marker = module.start_marker(self.leases, self.target['studio'], self.target['teamspace'], os.getpid())
            module.confirm_start(self.leases, marker)
        wrapper = self.record['child']
        module.record(self.job, self.leases, 'job_1', self.record['command_sha256'], self.target,
                      wrapper, marker)
        return module

    def settle(self, module, response=None):
        with patch.object(module, 'provider', side_effect=response or self.provider):
            return module.release(self.root, 'job_1', self.cleanup_record)

    def provider(self, capacity, action):
        if action == 'status':
            return {'status': 'Running', 'remote_boot_id': 'remote-original-boot'}
        if action == 'stop':
            return {'status': 'Stopped'}
        raise AssertionError(action)

    def test_exact_reservation_is_archived_and_other_unknown_jobs_are_preserved(self):
        module = self.capacity(owned=True)
        other = self.leases / '.unverifiable-job_other'
        other.write_text('~/offload/job_other\n')
        other.chmod(0o600)
        result = self.settle(module)
        self.assertTrue(result['capacity_released'])
        self.assertEqual(result['studio_state'], 'retained_shared')
        self.assertFalse(self.reservation.exists())
        self.assertTrue(other.exists())
        self.assertTrue((self.leases / '.started').exists())
        self.assertEqual((self.job / 'resources/reservation').read_text(), '~/offload/job_1\n')

    def test_owned_last_job_stops_exact_studio_and_records_verified_result(self):
        module = self.capacity(owned=True)
        calls = []
        def stopped(capacity, action):
            calls.append(action)
            if action == 'status':
                return {'status': 'Stopped'} if 'stop' in calls else self.provider(capacity, action)
            return self.provider(capacity, action)
        result = self.settle(module, stopped)
        self.assertEqual(result['studio_state'], 'stopped')
        self.assertTrue(result['capacity_released'])
        self.assertEqual(calls, ['status', 'stop', 'status'])
        self.assertFalse((self.leases / '.started').exists())

    def test_empty_legacy_marker_external_owner_and_changed_boot_never_stop(self):
        module = self.capacity()
        (self.leases / '.started').write_text('')
        with patch.object(module, 'provider') as provider:
            result = module.release(self.root, 'job_1', self.cleanup_record)
        provider.assert_not_called()
        self.assertEqual(result['studio_state'], 'retained_external')
        self.assertTrue((self.leases / '.started').exists())

    def test_changed_remote_boot_refuses_stop_but_releases_only_completed_job_capacity(self):
        module = self.capacity(owned=True)
        calls = []
        def changed(capacity, action):
            calls.append(action)
            return {'status': 'Running', 'remote_boot_id': 'different-boot'}
        result = self.settle(module, changed)
        self.assertEqual(result['studio_state'], 'retained_external')
        self.assertEqual(calls, ['status'])
        self.assertTrue(result['capacity_released'])

    def test_failed_or_ambiguous_stop_never_repeats_stop_without_reconciliation(self):
        module = self.capacity(owned=True)
        def failed(capacity, action):
            if action == 'stop':
                raise RuntimeError('provider stop response lost verbatim')
            return self.provider(capacity, action)
        with self.assertRaisesRegex(RuntimeError, 'lost verbatim'):
            self.settle(module, failed)
        calls = []
        def running(capacity, action):
            calls.append(action)
            return self.provider(capacity, action)
        result = self.settle(module, running)
        self.assertEqual(calls, ['status'])
        self.assertEqual(result['studio_state'], 'stop_unknown')
        self.assertTrue((self.leases / '.started').exists())
        result = self.settle(module, lambda capacity, action: {'status': 'Stopped'})
        self.assertEqual(result['studio_state'], 'stopped')

    def test_wrong_reservation_missing_cleanup_wrong_job_and_live_wrapper_never_release(self):
        module = self.capacity()
        self.reservation.write_text('~/offload/foreign\n')
        with self.assertRaisesRegex(ValueError, 'reservation'):
            self.settle(module)
        self.assertTrue(self.reservation.exists())
        self.reservation.write_text('~/offload/job_1\n')
        with self.assertRaises(ValueError):
            module.release(self.root, 'job_1', {**self.cleanup_record, 'intent_sha256': 'c' * 64})
        with patch.object(module, 'absent', side_effect=ValueError('original wrapper still present')), \
                self.assertRaisesRegex(ValueError, 'still present'):
            self.settle(module)
        self.assertTrue(self.reservation.exists())

    def test_private_path_symlink_and_foreign_marker_cannot_be_recorded(self):
        module = self.module()
        self.leases.chmod(0o775)
        with self.assertRaisesRegex(ValueError, 'private'):
            module.start_marker(self.leases, 'existing-studio', 'approved/teamspace', os.getpid())
        self.leases.chmod(0o700)
        marker = module.start_marker(self.leases, 'existing-studio', 'approved/teamspace', os.getpid())
        with self.assertRaises(ValueError):
            module.record(self.job, self.leases, 'job_1', self.record['command_sha256'], self.target,
                          self.record['child'], {**marker, 'studio': 'foreign-studio'})

    def test_replayed_release_never_releases_new_reservation_or_rechecks_mutable_provider(self):
        module = self.capacity()
        first = self.settle(module)
        self.reservation.write_text('~/offload/new-work\n')
        with patch.object(module, 'provider') as provider:
            result = module.release(self.root, 'job_1', self.cleanup_record)
        provider.assert_not_called()
        self.assertEqual(result, first)
        self.assertEqual(self.reservation.read_text(), '~/offload/new-work\n')

    def test_crash_after_unlink_preserves_archived_identity_and_resumes_same_release(self):
        module = self.capacity()
        original = execution.persist
        def failed(directory, name, value):
            if name == 'release.json':
                raise OSError('release evidence storage unavailable')
            return original(directory, name, value)
        with patch.object(execution, 'persist', side_effect=failed), self.assertRaisesRegex(OSError, 'storage unavailable'):
            self.settle(module)
        self.assertFalse(self.reservation.exists())
        self.assertTrue((self.job / 'resources/reservation').exists())
        result = self.settle(module)
        self.assertTrue(result['capacity_released'])

    def test_live_numeric_lease_and_unknown_process_are_never_removed_or_stopped(self):
        module = self.capacity(owned=True)
        live = self.leases / str(os.getpid())
        live.write_text(str(os.getpid()))
        live.chmod(0o600)
        with patch.object(module, 'provider') as provider:
            result = module.release(self.root, 'job_1', self.cleanup_record)
        provider.assert_not_called()
        self.assertEqual(result['studio_state'], 'retained_shared')
        self.assertTrue(live.exists())

    def test_capacity_wrapper_or_command_cannot_change_from_original_execution(self):
        module = self.capacity()
        path = self.job / 'capacity.json'
        value = json.loads(path.read_text())
        for change in ({'command_sha256': 'b' * 64}, {'wrapper': execution.process(os.getpid())},
                       {'target': {**value['target'], 'remote_dir': 'offload/foreign'}}):
            path.write_text(json.dumps({**value, **change}))
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.settle(module)
        self.assertTrue(self.reservation.exists())

    def test_original_release_intent_precedes_unlink_and_survives_changed_claim(self):
        module = self.capacity()
        original = execution.persist
        def fail_intent(directory, name, value):
            if name == 'intent.json':
                raise OSError('resource intent disk unavailable')
            return original(directory, name, value)
        with patch.object(execution, 'persist', side_effect=fail_intent), self.assertRaisesRegex(OSError, 'disk unavailable'):
            self.settle(module)
        self.assertTrue(self.reservation.exists())

    def test_stop_unknown_shared_operation_is_not_repeated_by_another_completed_job(self):
        module = self.capacity(owned=True)
        def failed(capacity, action):
            if action == 'stop':
                raise RuntimeError('lost stop')
            return self.provider(capacity, action)
        with self.assertRaises(RuntimeError):
            self.settle(module, failed)
        self.assertTrue((self.leases / '.stop-intent').exists())

    def test_null_resource_result_and_symlink_reservation_fail_closed(self):
        module = self.capacity()
        directory = self.job / 'resources'
        directory.mkdir(mode=0o700)
        (directory / 'release.json').write_text('null')
        (directory / 'release.json').chmod(0o600)
        with self.assertRaises(ValueError):
            self.settle(module)
        (directory / 'release.json').unlink()
        self.reservation.unlink()
        other = self.leases / 'foreign'
        other.write_text('~/offload/job_1\n')
        self.reservation.symlink_to(other)
        with self.assertRaisesRegex(ValueError, 'symlink'):
            self.settle(module)
        self.assertTrue(other.exists())

    def test_wrapper_observation_state_is_not_part_of_process_incarnation(self):
        module = self.capacity()
        path = self.job / 'capacity.json'
        value = json.loads(path.read_text())
        path.write_text(json.dumps({**value, 'wrapper': {**value['wrapper'], 'state': 'S'}}))
        self.assertTrue(self.settle(module)['capacity_released'])

    def test_verified_stop_archives_only_original_start_and_stop_markers(self):
        module = self.capacity(owned=True)
        calls = []
        def stopped(capacity, action):
            calls.append(action)
            return {'status': 'Stopped'} if action == 'status' and 'stop' in calls else self.provider(capacity, action)
        self.settle(module, stopped)
        self.assertFalse((self.leases / '.stop-intent').exists())
        self.assertTrue((self.job / 'resources/studio-start.json').exists())
        self.assertTrue((self.job / 'resources/studio-stop.json').exists())

    def test_shared_marker_with_foreign_remote_boot_never_claims_already_stopped_studio(self):
        module = self.capacity(owned=True)
        execution.persist(self.leases, '.stop-intent', {'start_marker': {},
                                                      'remote_boot_id': 'foreign'})
        result = self.settle(module, lambda capacity, action: {'status': 'Stopped'})
        self.assertEqual(result['studio_state'], 'retained_external')
        self.assertTrue((self.leases / '.started').exists())

    def test_foreign_studio_capacity_target_does_not_override_original_transport(self):
        module = self.capacity()
        path = self.job / 'capacity.json'
        value = json.loads(path.read_text())
        path.write_text(json.dumps({**value, 'target': {**value['target'], 'studio': 'foreign'}}))
        with self.assertRaises(ValueError):
            self.settle(module)
        self.assertTrue(self.reservation.exists())

    def test_null_current_start_marker_and_malformed_target_are_rejected_without_stop(self):
        module = self.capacity(owned=True)
        (self.leases / '.started').write_text('null')
        with patch.object(module, 'provider') as provider:
            result = module.release(self.root, 'job_1', self.cleanup_record)
        provider.assert_not_called()
        self.assertEqual(result['studio_state'], 'retained_external')

    def test_release_error_after_reservation_archive_preserves_literal_failure_receipt(self):
        module = self.capacity(owned=True)
        with self.assertRaisesRegex(RuntimeError, 'provider521 verbatim'):
            self.settle(module, lambda capacity, action: (_ for _ in ()).throw(RuntimeError('provider521 verbatim')))
        failures = list((self.job / 'resources').glob('failure-*.json'))
        self.assertEqual(len(failures), 1)
        self.assertIn('provider521 verbatim', failures[0].read_text())
        self.assertTrue((self.job / 'resources/reservation').exists())

    def test_shared_stop_started_is_durable_before_provider_call(self):
        module = self.capacity(owned=True)
        def failed(capacity, action):
            if action == 'stop':
                self.assertTrue((self.leases / '.stop-intent').exists())
                self.assertTrue((self.job / 'resources/intent.json').exists())
                raise RuntimeError('provider response lost')
            return self.provider(capacity, action)
        with self.assertRaisesRegex(RuntimeError, 'response lost'):
            self.settle(module, failed)

    def test_capacity_remote_boot_is_checked_against_exact_command_receipt(self):
        module = self.capacity(owned=True)
        path = self.job / 'capacity.json'
        value = json.loads(path.read_text())
        path.write_text(json.dumps({**value, 'target': {**value['target'], 'remote_boot_id': 'foreign'}}))
        with self.assertRaisesRegex(ValueError, 'remote.*boot'):
            self.settle(module)
        self.assertTrue(self.reservation.exists())

    def test_stale_numeric_lease_is_unknown_remote_work_and_never_authorizes_stop(self):
        module = self.capacity(owned=True)
        stale = self.leases / '99999999'
        stale.write_text('99999999')
        stale.chmod(0o600)
        with patch.object(module, 'provider') as provider:
            result = module.release(self.root, 'job_1', self.cleanup_record)
        provider.assert_not_called()
        self.assertEqual(result['studio_state'], 'retained_shared')
        self.assertTrue(stale.exists())

    def test_verified_stop_crash_before_final_release_keeps_same_studio_result(self):
        module = self.capacity(owned=True)
        original = execution.persist
        calls = []
        def stopped(capacity, action):
            calls.append(action)
            return {'status': 'Stopped'} if action == 'status' and 'stop' in calls else self.provider(capacity, action)
        def failed(directory, name, value):
            if name == 'release.json':
                raise OSError('release commit unavailable')
            return original(directory, name, value)
        with patch.object(execution, 'persist', side_effect=failed), self.assertRaises(OSError):
            self.settle(module, stopped)
        with patch.object(module, 'provider') as provider:
            result = module.release(self.root, 'job_1', self.cleanup_record)
        provider.assert_not_called()
        self.assertEqual(result['studio_state'], 'stopped')

    def test_recreated_matching_reservation_is_not_original_capacity(self):
        module = self.capacity()
        self.reservation.rename(self.leases / 'original-reservation')
        self.reservation.write_text('~/offload/job_1\n')
        self.reservation.chmod(0o600)
        with self.assertRaisesRegex(ValueError, 'reservation'):
            self.settle(module)
        self.assertTrue(self.reservation.exists())

    def test_crash_during_shared_marker_archive_finishes_only_same_marker_cleanup(self):
        module = self.capacity(owned=True)
        original = execution.persist
        calls = []
        def stopped(capacity, action):
            calls.append(action)
            return {'status': 'Stopped'} if action == 'status' and 'stop' in calls else self.provider(capacity, action)
        def failed(directory, name, value):
            if name == 'studio-stop.json':
                raise OSError('marker archive unavailable')
            return original(directory, name, value)
        with patch.object(execution, 'persist', side_effect=failed), self.assertRaises(OSError):
            self.settle(module, stopped)
        self.assertTrue((self.leases / '.stop-intent').exists())
        self.settle(module)
        self.assertFalse((self.leases / '.stop-intent').exists())

    def test_unconfirmed_native_start_never_authorizes_studio_stop(self):
        module = self.module()
        start = module.start_marker(self.leases, self.target['studio'], self.target['teamspace'], os.getpid())
        module.record(self.job, self.leases, 'job_1', self.record['command_sha256'], self.target,
                      self.record['child'], start)
        with patch.object(module, 'provider') as provider:
            result = module.release(self.root, 'job_1', self.cleanup_record)
        provider.assert_not_called()
        self.assertEqual(result['studio_state'], 'retained_external')

    def test_null_stop_marker_blocks_stop_and_is_preserved(self):
        module = self.capacity(owned=True)
        (self.leases / '.stop-intent').write_text('null')
        (self.leases / '.stop-intent').chmod(0o600)
        with patch.object(module, 'provider') as provider:
            result = module.release(self.root, 'job_1', self.cleanup_record)
        provider.assert_not_called()
        self.assertEqual(result['studio_state'], 'retained_external')
        self.assertTrue((self.leases / '.stop-intent').exists())


if __name__ == '__main__':
    unittest.main()
