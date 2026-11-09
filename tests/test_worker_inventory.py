"""Native custody transfer is one retained slot, never an absent-resource release."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import worker_pool

HOST = {'kind': 'local', 'id': 'local'}
OLD = {'runId': 'run_1', 'dispatchId': 'ctx_old', 'taskId': 'task_old',
       'workerState': 'succeeded', 'dispatchStatus': 'completed', 'terminalState': 'retained',
       'agentTerminalHandle': 'term_1', 'resource': None,
       'projection': {'host': HOST, 'workspace': {'id': 'repo::/worker'}}}
CURRENT = {'runId': 'run_1', 'dispatchId': 'ctx_current', 'taskId': 'task_current',
           'workerState': 'ready', 'dispatchStatus': 'dispatched', 'terminalState': 'retained',
           'agentTerminalHandle': 'term_1', 'projection': {'host': HOST},
           'resource': {'id': 'wtr_1', 'ownershipState': 'owned', 'releaseState': 'retained',
           'originDispatchId': 'ctx_old', 'ownerDispatchId': 'ctx_current',
           'terminalHandle': 'term_1', 'worktreeId': 'repo::/worker',
           'endpointId': 'endpoint_1', 'endpointIncarnation': 'pty_1:incarnation_1'}}


class WorkerInventoryTest(unittest.TestCase):
    def test_native_origin_owner_transfer_keeps_one_live_occupied_resource(self):
        result = worker_pool.occupancy([CURRENT, OLD], 'run_1')
        self.assertEqual(result, {('endpoint_1', 'term_1'): 'repo::/worker'})
        self.assertIsNone(OLD['resource'])
        self.assertEqual(CURRENT['resource']['releaseState'], 'retained')

    def test_missing_foreign_or_ambiguous_transfer_preserves_unknown_capacity(self):
        for key, value in [('originDispatchId', 'foreign'), ('ownerDispatchId', 'foreign'),
                           ('terminalHandle', 'term_other'), ('worktreeId', 'repo::/other'),
                           ('endpointIncarnation', None), ('ownershipState', 'unknown'),
                           ('releaseState', 'release_unknown')]:
            row = {**CURRENT, 'resource': {**CURRENT['resource'], key: value}}
            with self.subTest(key=key):
                occupied = worker_pool.occupancy([row, OLD], 'run_1')
                self.assertIn((None, 'term_1'), occupied)
        foreign = {**CURRENT, 'projection': {'host': {'kind': 'remote', 'id': 'other'}}}
        self.assertIn((None, 'term_1'), worker_pool.occupancy([foreign, OLD], 'run_1'))
        ambiguous = {**CURRENT, 'resource': {**CURRENT['resource'], 'id': 'wtr_other', 'endpointId': 'endpoint_other'}}
        self.assertIn((None, 'term_1'), worker_pool.occupancy([CURRENT, ambiguous, OLD], 'run_1'))

    def test_unsettled_history_or_existing_resource_never_uses_an_origin_hint(self):
        active = {**OLD, 'dispatchStatus': 'dispatched'}
        self.assertIn((None, 'term_1'), worker_pool.occupancy([CURRENT, active], 'run_1'))
        existing = {**OLD, 'resource': {'endpointId': 'other', 'terminalHandle': 'term_1',
                                     'worktreeId': 'repo::/worker'}}
        self.assertEqual(len(worker_pool.occupancy([CURRENT, existing], 'run_1')), 2)

    def test_malformed_projection_never_turns_into_transfer_proof(self):
        for projection in ('not-a-record', {'host': HOST, 'workspace': 'not-a-record'}):
            row = {**OLD, 'projection': projection}
            with self.subTest(projection=projection):
                self.assertIn((None, 'term_1'), worker_pool.occupancy([CURRENT, row], 'run_1'))

    def test_exact_native_origin_release_reclaims_only_the_same_proven_resource(self):
        released = {**CURRENT, 'dispatchStatus': 'failed', 'terminalState': 'released',
                    'projection': {**CURRENT['projection'], 'liveness': {'verdict': 'exited', 'source': 'resource_release'}},
                    'resource': {**CURRENT['resource'], 'ownershipState': 'released', 'releaseState': 'released',
                                 'releaseCompletedAt': 'native-release-time'}}
        self.assertEqual(worker_pool.occupancy([released, OLD], 'run_1'), {})
        for changed in ({'liveness': {'verdict': 'live', 'source': 'agent_status'}}, {'liveness': {}},
                        {'liveness': 'not-a-record'}):
            row = {**released, 'projection': {**released['projection'], **changed}}
            with self.subTest(changed=changed):
                self.assertIn((None, 'term_1'), worker_pool.occupancy([row, OLD], 'run_1'))
        unproven = {**released, 'resource': {**released['resource'], 'releaseCompletedAt': None}}
        self.assertIn((None, 'term_1'), worker_pool.occupancy([unproven, OLD], 'run_1'))
