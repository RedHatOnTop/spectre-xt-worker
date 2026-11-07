"""Native wake delivery preserves exact ownership and ambiguous requests."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import native
from superagent_queue.store import Journal

TARGET = {'run_id': 'run_1', 'terminal_handle': 'term_1', 'worktree_id': 'repo::/work',
          'incarnation_id': 'process_1', 'text': 'Continue the existing authorized task.'}


def inventory(state='done'):
    return {'runtime': {'runtime': {'reachable': True, 'state': 'ready'}},
            'runs': [{'id': 'run_1', 'legacy': 0, 'coordinator_handle': 'term_1'}],
            'terminals': [{'handle': 'term_1', 'incarnationId': 'process_1', 'connected': True,
                           'writable': True, 'worktreeId': 'repo::/work', 'paneKey': 'pane_1'}],
            'worktrees': [{'worktreeId': 'repo::/work', 'agents': [{'paneKey': 'pane_1', 'state': state}]}]}


def receipt(stage='turn_started'):
    return {'ok': True, 'result': {'send': {'handle': 'term_1', 'accepted': True,
            'prompt': {'requestId': 'request_1', 'processIncarnation': 'process_1',
                       'stages': [stage], 'observation': 'supported'}},
            'mutation': {'requestId': 'request_1'}}}


class NativeDeliveryTest(unittest.TestCase):
    def test_idle_snapshot_cannot_send_after_owner_started_work_during_source_reads(self):
        with patch.object(native.observe, 'collect', return_value=inventory('working')) as refresh, \
                patch.object(native.subprocess, 'run') as send:
            report = native.deliver(self.queue, self.claim, '/orca', now=100, snapshot=inventory())
        send.assert_not_called()
        self.assertEqual(report['status'], 'deferred')
        refresh.assert_called_once_with('/orca', run_ids=('run_1',), include_workers=False)

    def test_send_preflight_rechecks_incarnation_without_rebinding(self):
        changed = {**inventory(), 'terminals': [{**inventory()['terminals'][0], 'incarnationId': 'new'}]}
        with patch.object(native.observe, 'collect', return_value=changed), \
                patch.object(native.subprocess, 'run') as send:
            report = native.deliver(self.queue, self.claim, '/orca', now=100, snapshot=inventory())
        send.assert_not_called()
        self.assertEqual(report['status'], 'attention')

    def test_delivery_reads_only_the_current_claim_not_the_retained_history(self):
        with patch.object(self.queue, 'effects', side_effect=AssertionError('full effect scan')), \
                patch.object(self.queue, 'attempts', side_effect=AssertionError('attempt history scan')), \
                patch.object(native.observe, 'collect', return_value=inventory()), \
                patch('superagent_queue.native.subprocess.run',
                      return_value=subprocess.CompletedProcess([], 0, json.dumps(receipt()), '')):
            report = native.deliver(self.queue, self.claim, '/orca', now=100, snapshot=inventory())
        self.assertEqual(report['status'], 'delivered')

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.queue = Journal(Path(self.directory.name) / 'private' / 'journal.sqlite3')
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.queue.close)
        self.queue.ingest('operator', 'event_1', {}, 'wake_1', 'wake', 'run_1', TARGET, now=100)
        self.claim = self.queue.claim('driver_1', now=100)

    def deliver(self, response=None, data=None):
        result = subprocess.CompletedProcess([], 0, json.dumps(response or receipt()), '')
        with patch('superagent_queue.native.observe.collect', return_value=data or inventory()), \
                patch('superagent_queue.native.subprocess.run', return_value=result) as execute:
            report = native.deliver(self.queue, self.claim, '/orca', now=100)
        return report, execute

    def test_same_coordinator_gets_one_wake_and_started_receipt(self):
        report, execute = self.deliver()
        self.assertEqual(report['status'], 'delivered')
        self.assertEqual(execute.call_count, 1)
        argv = execute.call_args.args[0]
        self.assertEqual(argv[:5], ['/orca', 'terminal', 'send', '--terminal', 'term_1'])
        self.assertIn('--wait-submit', argv)
        self.assertNotIn('worker-start', argv)
        self.assertNotIn('terminal create', ' '.join(argv))
        self.assertEqual(self.queue.attempts('wake_1')[0]['receipt'], receipt())

    def test_working_coordinator_is_not_interrupted_or_duplicated(self):
        report, execute = self.deliver(data=inventory('working'))
        execute.assert_not_called()
        self.assertEqual(report['status'], 'deferred')
        self.assertEqual(self.queue.effects()[0]['failures'], 0)

    def test_changed_incarnation_requires_attention_without_send(self):
        data = inventory()
        data = {**data, 'terminals': [{**data['terminals'][0], 'incarnationId': 'process_2'}]}
        report, execute = self.deliver(data=data)
        execute.assert_not_called()
        self.assertEqual(report['status'], 'attention')
        self.assertIn('incarnation', self.queue.effects()[0]['error'])

    def test_missing_native_owner_never_creates_replacement(self):
        for field in ('runs', 'worktrees', 'terminals'):
            with self.subTest(field=field):
                data = {**inventory(), field: []}
                with self.assertRaisesRegex(ValueError, 'native'):
                    native.validate_target(TARGET, data)

    def test_unknown_agent_state_is_not_idle(self):
        data = inventory('unknown')
        report, execute = self.deliver(data=data)
        execute.assert_not_called()
        self.assertEqual(report['status'], 'deferred')

    def test_input_accepted_is_retained_not_claimed_started(self):
        report, _ = self.deliver(response=receipt('input_accepted'))
        self.assertEqual(report['status'], 'reconciling')
        self.assertEqual(self.queue.summary()['effects']['reconciling'], 1)
        self.assertEqual(self.queue.attempts('wake_1')[0]['receipt'], receipt('input_accepted'))

    def test_accepted_request_replay_observes_same_id_after_restart(self):
        self.deliver(response=receipt('input_accepted'))
        recovered = self.queue.reconciliation_claims()[0]
        result = subprocess.CompletedProcess([], 0, json.dumps(receipt()), '')
        with patch('superagent_queue.native.observe.collect', return_value=inventory()), \
                patch('superagent_queue.native.subprocess.run', return_value=result) as execute:
            report = native.deliver(self.queue, recovered, '/orca', now=102)
        argv = execute.call_args.args[0]
        self.assertEqual(argv[argv.index('--retry-request') + 1], 'request_1')
        self.assertEqual(report['status'], 'delivered')

    def test_lost_response_without_id_never_blindly_replays(self):
        with patch('superagent_queue.native.observe.collect', return_value=inventory()), \
                patch('superagent_queue.native.subprocess.run', side_effect=subprocess.TimeoutExpired('/orca', 30)):
            report = native.deliver(self.queue, self.claim, '/orca', now=100)
        self.assertEqual(report['status'], 'reconciling')
        recovered = self.queue.reconciliation_claims()[0]
        with patch('superagent_queue.native.subprocess.run') as execute:
            report = native.deliver(self.queue, recovered, '/orca', now=101)
        execute.assert_not_called()
        self.assertEqual(report['status'], 'reconciling')

    def test_invalid_native_json_is_ambiguous_not_retryable(self):
        result = subprocess.CompletedProcess([], 0, 'not-json', 'output may have been lost')
        with patch('superagent_queue.native.observe.collect', return_value=inventory()), \
                patch('superagent_queue.native.subprocess.run', return_value=result):
            native.deliver(self.queue, self.claim, '/orca', now=100)
        self.assertEqual(self.queue.summary()['effects']['reconciling'], 1)
        self.assertIn('not-json', self.queue.effects()[0]['error'])

    def test_malformed_native_envelope_keeps_the_claim_reconciling(self):
        for response in ({'ok': True, 'result': []}, {'ok': True, 'result': {'send': []}},
                         {'ok': True, 'result': {'send': {'prompt': []}}}):
            with self.subTest(response=response):
                self.assertEqual(native.prompt_receipt(response), {})

    def test_replayed_receipt_must_match_original_terminal_and_request(self):
        response = receipt()
        response = {**response, 'result': {**response['result'], 'send': {
            **response['result']['send'], 'handle': 'other_terminal'}}}
        report, _ = self.deliver(response=response)
        self.assertEqual(report['status'], 'reconciling')
        self.assertIsNone(self.queue.claim('another_driver', now=10000))

    def test_unmatched_replay_request_retains_original_identity_for_reconciliation(self):
        original = receipt('input_accepted')
        self.deliver(response=original)
        response = receipt('input_accepted')
        response = {**response, 'result': {**response['result'], 'send': {
            **response['result']['send'], 'prompt': {
                **response['result']['send']['prompt'], 'requestId': 'other_request'}}}}
        result = subprocess.CompletedProcess([], 0, json.dumps(response), '')
        with patch('superagent_queue.native.observe.collect', return_value=inventory()), \
                patch('superagent_queue.native.subprocess.run', return_value=result):
            report = native.deliver(self.queue, self.claim, '/orca', now=102)
        self.assertEqual(report['status'], 'reconciling')
        self.assertEqual(self.queue.claim_record(self.claim)['receipt'], original)

    def test_unreachable_and_legacy_runtime_cannot_authorize_wake(self):
        for data in ({**inventory(), 'runtime': {}},
                     {**inventory(), 'runs': [{**inventory()['runs'][0], 'legacy': 1}]}):
            with self.subTest(data=data), self.assertRaises((ValueError, RuntimeError)):
                native.validate_target(TARGET, data)

    def test_settled_or_superseded_claim_cannot_send_again(self):
        self.queue.complete(self.claim, {}, now=100)
        with patch('superagent_queue.native.subprocess.run') as execute:
            report = native.deliver(self.queue, self.claim, '/orca', now=101)
        execute.assert_not_called()
        self.assertEqual(report['status'], 'stale_claim')

    def test_new_driver_waits_for_reconciliation_owner_before_replay(self):
        self.deliver(response=receipt('input_accepted'))
        self.assertIsNone(self.queue.claim('new_driver', now=100000))
        self.assertEqual(len(self.queue.reconciliation_claims()), 1)

    def test_marked_input_is_reconciled_from_native_started_snapshot_without_replay(self):
        text = '[spectre-delivery:event_2] Continue the existing authorized task.'
        payload = {**TARGET, 'text': text}
        self.queue.ingest('operator', 'event_2', {}, 'wake_2', 'wake', 'run_2', payload, now=100)
        self.queue.complete(self.claim, {}, now=100)
        claim = self.queue.claim('driver_1', now=100)
        self.queue.record_receipt(claim, receipt('input_accepted'))
        self.queue.uncertain(claim, 'turn start unproven', now=100)
        data = inventory('working')
        data = {**data, 'worktrees': [{**data['worktrees'][0], 'agents': [{
            **data['worktrees'][0]['agents'][0], 'prompt': text, 'stateStartedAt': 101000}]}]}
        with patch('superagent_queue.native.observe.collect', return_value=data), \
                patch('superagent_queue.native.subprocess.run') as execute:
            report = native.deliver(self.queue, claim, '/orca', now=102)
        execute.assert_not_called()
        self.assertEqual(report['status'], 'delivered')
        saved = self.queue.attempts('wake_2')[0]['receipt']
        self.assertEqual(saved['result']['send']['prompt']['stages'], ['input_accepted'])
        self.assertEqual(saved['delivery_evidence']['source'], 'native_agent_snapshot')

    def test_marked_send_captures_fast_completion_before_later_input_replaces_snapshot(self):
        text = '[spectre-delivery:event_2] Continue the existing authorized task.'
        payload = {**TARGET, 'text': text}
        self.queue.complete(self.claim, {}, now=100)
        self.queue.ingest('operator', 'event_2', {}, 'wake_2', 'wake', 'run_1', payload, now=100)
        claim = self.queue.claim('driver_1', now=100)
        done = inventory('done')
        done = {**done, 'worktrees': [{**done['worktrees'][0], 'agents': [{
            **done['worktrees'][0]['agents'][0], 'prompt': text, 'stateStartedAt': 101000,
            'lastAssistantMessage': 'Context reviewed; no permission changed.', 'interrupted': False}]}]}
        result = subprocess.CompletedProcess([], 0, json.dumps(receipt('input_accepted')), '')
        with patch('superagent_queue.native.observe.collect', return_value=done) as collect, \
                patch('superagent_queue.native.subprocess.run', return_value=result) as execute:
            report = native.deliver(self.queue, claim, '/orca', now=102, snapshot=inventory())
        self.assertEqual(report['status'], 'delivered')
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(collect.call_count, 2)
        self.assertTrue(all(call.kwargs == {'run_ids': ('run_1',), 'include_workers': False}
                            for call in collect.call_args_list))
        self.assertEqual(self.queue.attempts('wake_2')[0]['receipt']['delivery_evidence']['state'], 'done')

    def test_post_send_observation_failure_retains_original_request_without_replay(self):
        result = subprocess.CompletedProcess([], 0, json.dumps(receipt('input_accepted')), '')
        with patch('superagent_queue.native.observe.collect', side_effect=[inventory(), RuntimeError('post-send host unavailable')]), \
                patch('superagent_queue.native.subprocess.run', return_value=result) as execute:
            report = native.deliver(self.queue, self.claim, '/orca', now=102, snapshot=inventory())
        self.assertEqual(report['status'], 'reconciling')
        self.assertEqual(report['observation_error'], 'post-send host unavailable')
        self.assertEqual(execute.call_count, 1)
        self.assertEqual(self.queue.attempts('wake_1')[0]['receipt'], receipt('input_accepted'))

    def test_tick_snapshot_uses_only_scoped_control_refresh_before_send(self):
        result = subprocess.CompletedProcess([], 0, json.dumps(receipt()), '')
        with patch('superagent_queue.native.observe.collect', return_value=inventory()) as collect, \
                patch('superagent_queue.native.subprocess.run', return_value=result):
            report = native.deliver(self.queue, self.claim, '/orca', now=100, snapshot=inventory())
        collect.assert_called_once_with('/orca', run_ids=('run_1',), include_workers=False)
        self.assertEqual(report['status'], 'delivered')

    def test_preflight_outage_can_retry_without_terminal_mutation(self):
        with patch('superagent_queue.native.observe.collect', side_effect=RuntimeError('host_unavailable verbatim')), \
                patch('superagent_queue.native.subprocess.run') as execute:
            report = native.deliver(self.queue, self.claim, '/orca', now=100)
        execute.assert_not_called()
        self.assertEqual(report['status'], 'retry_wait')
        self.assertEqual(self.queue.effects()[0]['error'], 'host_unavailable verbatim')

    def test_target_payload_requires_exact_bounded_identifiers_and_text(self):
        for change in ({'text': ''}, {'incarnation_id': ''}, {'unknown': True}, {'text': 'a\x00b'}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                native.validate_payload({**TARGET, **change})


if __name__ == '__main__':
    unittest.main()
