"""Correlate a native started turn without inventing submission stages."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import reconcile

TEXT = '[spectre-delivery:event_1] Continue the existing authorized task.'
TARGET = {'run_id': 'run_1', 'terminal_handle': 'term_1', 'worktree_id': 'repo::/work',
          'incarnation_id': 'process_1', 'text': TEXT}
RECEIPT = {'ok': True, 'result': {'send': {'handle': 'term_1', 'accepted': True,
           'prompt': {'requestId': 'request_1', 'processIncarnation': 'process_1',
                      'stages': ['input_accepted']}}}}


def evidence():
    return {'runtime': {'runtime': {'reachable': True, 'state': 'ready'}},
            'runs': [{'id': 'run_1', 'legacy': 0, 'coordinator_handle': 'term_1'}],
            'terminals': [{'handle': 'term_1', 'incarnationId': 'process_1',
                           'worktreeId': 'repo::/work', 'connected': True, 'writable': True,
                           'paneKey': 'pane_1'}],
            'worktrees': [{'worktreeId': 'repo::/work', 'agents': [{'paneKey': 'pane_1',
                          'state': 'working', 'stateStartedAt': 101000, 'updatedAt': 101000,
                          'prompt': TEXT}]}]}


class SubmissionReconciliationTest(unittest.TestCase):
    def test_native_same_prompt_started_after_claim_proves_delivery_only(self):
        result = reconcile.started(TARGET, RECEIPT, evidence(), 100, now=102)
        self.assertEqual(result['source'], 'native_agent_snapshot')
        self.assertEqual(result['request_id'], 'request_1')
        self.assertEqual(result['native_stages'], ['input_accepted'])
        self.assertNotIn('turn_started', result['native_stages'])

    def test_fast_completed_turn_proves_delivery_without_replaying_input(self):
        data = evidence()
        agent = {**data['worktrees'][0]['agents'][0], 'state': 'done',
                 'lastAssistantMessage': 'Context received; no new work authorized.', 'interrupted': False}
        data = {**data, 'worktrees': [{**data['worktrees'][0], 'agents': [agent]}]}
        result = reconcile.started(TARGET, RECEIPT, data, 100, now=102)
        self.assertEqual(result['state'], 'done')
        self.assertTrue(result['delivery_only'])
        self.assertEqual(result['native_stages'], ['input_accepted'])

    def test_done_without_response_or_interrupted_cannot_settle_delivery(self):
        for change in ({'lastAssistantMessage': None}, {'lastAssistantMessage': ''},
                       {'lastAssistantMessage': 'Old output', 'interrupted': True},
                       {'lastAssistantMessage': 'Old output', 'stateStartedAt': 99000}):
            data = evidence()
            agent = {**data['worktrees'][0]['agents'][0], 'state': 'done',
                     'interrupted': False, **change}
            data = {**data, 'worktrees': [{**data['worktrees'][0], 'agents': [agent]}]}
            self.assertIsNone(reconcile.started(TARGET, RECEIPT, data, 100, now=102))

    def test_stale_turn_cannot_settle_new_delivery(self):
        self.assertIsNone(reconcile.started(TARGET, RECEIPT, evidence(), 102, now=103))

    def test_other_prompt_or_common_prefix_is_not_delivery(self):
        for prompt in ('different', '[spectre-delivery:event_2] Continue the existing authorized task.'):
            data = evidence()
            data = {**data, 'worktrees': [{**data['worktrees'][0], 'agents': [
                {**data['worktrees'][0]['agents'][0], 'prompt': prompt}]}]}
            self.assertIsNone(reconcile.started(TARGET, RECEIPT, data, 100, now=102))

    def test_legacy_unmarked_prompt_requires_explicit_review(self):
        target = {**TARGET, 'text': 'Continue the existing authorized task.'}
        self.assertIsNone(reconcile.started(target, RECEIPT, evidence(), 100, now=102))

    def test_future_or_missing_timestamp_does_not_prove_delivery(self):
        for stamp in (None, 103000, float('nan'), True):
            data = evidence()
            data = {**data, 'worktrees': [{**data['worktrees'][0], 'agents': [
                {**data['worktrees'][0]['agents'][0], 'stateStartedAt': stamp}]}]}
            self.assertIsNone(reconcile.started(TARGET, RECEIPT, data, 100, now=102))

    def test_no_receipt_or_unmatched_receipt_cannot_settle(self):
        for receipt in ({}, {'ok': False}, {**RECEIPT, 'result': {}}):
            self.assertIsNone(reconcile.started(TARGET, receipt, evidence(), 100, now=102))

    def test_changed_native_owner_or_incarnation_never_confirms_delivery(self):
        for field in ('runs', 'worktrees', 'terminals'):
            data = {**evidence(), field: []}
            self.assertIsNone(reconcile.started(TARGET, RECEIPT, data, 100, now=102))
        data = evidence()
        data = {**data, 'terminals': [{**data['terminals'][0], 'incarnationId': 'process_2'}]}
        self.assertIsNone(reconcile.started(TARGET, RECEIPT, data, 100, now=102))

    def test_unknown_idle_and_permission_states_are_not_started_proof(self):
        for state in ('done', 'unknown', 'permission', 'idle'):
            data = evidence()
            data = {**data, 'worktrees': [{**data['worktrees'][0], 'agents': [
                {**data['worktrees'][0]['agents'][0], 'state': state}]}]}
            self.assertIsNone(reconcile.started(TARGET, RECEIPT, data, 100, now=102))


if __name__ == '__main__':
    unittest.main()
