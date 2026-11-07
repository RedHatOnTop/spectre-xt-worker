"""Continuity ticks survive restart and do not replay blocked work."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import driver, execution, sources
from superagent_queue.store import Journal

TARGET = {'run_id': 'run_1', 'terminal_handle': 'term_1', 'worktree_id': 'repo::/work',
          'incarnation_id': 'process_1', 'text': 'Resume only existing authorized work.'}
SETTINGS = {'schema_version': 1, 'coordinators': [TARGET]}


def inventory(state='done', since=99000):
    return {'runtime': {'runtime': {'reachable': True, 'state': 'ready'}},
            'runs': [{'id': 'run_1', 'legacy': 0, 'coordinator_handle': 'term_1'}],
            'terminals': [{'handle': 'term_1', 'incarnationId': 'process_1', 'connected': True,
                           'writable': True, 'worktreeId': 'repo::/work', 'paneKey': 'pane_1'}],
            'worktrees': [{'worktreeId': 'repo::/work', 'agents': [{'paneKey': 'pane_1',
                          'state': state, 'stateStartedAt': since, 'prompt': 'previous task'}]}],
            'workers': []}


class DriverTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'private' / 'journal.sqlite3'
        self.queue = Journal(self.path)
        self.addCleanup(self.queue.close)

    def test_idle_authorized_coordinator_gets_one_durable_event_per_turn(self):
        for _ in range(3):
            driver.discover(self.queue, SETTINGS, inventory(), {}, now=100)
        self.assertEqual(self.queue.summary()['events'], 1)
        claim = self.queue.claim('driver', now=100)
        self.assertTrue(claim.payload['text'].startswith('[spectre-delivery:'))
        self.assertIn(TARGET['text'], claim.payload['text'])

    def test_later_turn_end_generates_followup_without_duplicate_owner(self):
        driver.discover(self.queue, SETTINGS, inventory(), {}, now=100)
        self.queue.complete(self.queue.claim('driver', now=100), {}, now=101)
        driver.discover(self.queue, SETTINGS, inventory(since=102000), {}, now=103)
        self.assertEqual(self.queue.summary()['events'], 2)

    def test_working_unknown_and_permission_coordinator_are_not_woken(self):
        for state in ('working', 'unknown', 'permission'):
            driver.discover(self.queue, SETTINGS, inventory(state), {}, now=100)
        self.assertEqual(self.queue.summary()['events'], 0)

    def test_waiting_approval_has_no_periodic_model_wake(self):
        disposition = {'run_1': {'state': 'waiting_approval', 'gate_ids': ['gate_1']}}
        for now in (100, 1000, 100000):
            driver.discover(self.queue, SETTINGS, inventory(), disposition, now=now)
        self.assertEqual(self.queue.summary()['events'], 0)

    def test_explicit_approval_wait_omits_worker_history_but_keeps_current_source_reads(self):
        wait = {'run_1': {'state': 'waiting_approval', 'gate_ids': ['gate_1']}}
        with patch.object(driver.observe, 'collect', return_value=inventory()) as collect, \
                patch.object(driver, 'collect_context', return_value={'queued': 0, 'issues': []}) as sources, \
                patch.object(driver.observe, 'collect_workers') as workers:
            report = driver.tick(self.queue, SETTINGS, wait, '/orca', now=100)
        collect.assert_called_once_with('/orca', run_ids=('run_1',), include_workers=False)
        sources.assert_called_once()
        workers.assert_called_once_with('/orca', run_id='run_1')
        self.assertEqual(report['queued'], 0)

    def test_disposition_change_during_source_reads_restores_current_worker_observation(self):
        wait = {'run_1': {'state': 'waiting_approval', 'gate_ids': ['gate_1']}}

        def changed(*args):
            self.queue.publish_disposition('run_1', {'state': 'working'}, 0, {}, now=100)
            return {'queued': 0, 'issues': []}

        worker = {'runId': 'run_1', 'dispatchStatus': 'dispatched', 'terminalState': 'retained'}
        with patch.object(driver.observe, 'collect', return_value=inventory()) as collect, \
                patch.object(driver, 'collect_context', side_effect=changed), \
                patch.object(driver.observe, 'collect_workers', return_value=[worker]) as workers:
            report = driver.tick(self.queue, SETTINGS, wait, '/orca', now=100)
        collect.assert_called_once_with('/orca', run_ids=('run_1',), include_workers=False)
        self.assertEqual(workers.call_count, 2)
        workers.assert_called_with('/orca', run_id='run_1')
        self.assertEqual(report['queued'], 0)

    def test_one_eligible_run_keeps_worker_observation_for_automatic_resume_safety(self):
        second = {**TARGET, 'run_id': 'run_2', 'terminal_handle': 'term_2', 'worktree_id': 'repo::/other'}
        wait = {'run_1': {'state': 'waiting_approval', 'gate_ids': ['gate_1']}}
        with patch.object(driver.observe, 'collect', return_value=inventory()) as collect, \
                patch.object(driver.observe, 'collect_workers', return_value=[]), \
                patch.object(driver, 'collect_context', return_value={'queued': 0, 'issues': []}):
            driver.tick(self.queue, {'coordinators': [TARGET, second]}, wait, '/orca', now=100)
        collect.assert_called_once_with('/orca', run_ids=('run_1', 'run_2'), include_workers=True)

    def test_due_provider_retry_requires_worker_observation_while_other_waits_do_not(self):
        for value, now, expected in (
                ({'state': 'waiting_provider', 'retry_at': 101}, 100, False),
                ({'state': 'waiting_provider', 'retry_at': 101}, 101, True),
                ({'state': 'waiting_job', 'job_ids': ['job_1']}, 101, False),
                ({'state': 'finished'}, 101, False), ({'state': 'working'}, 101, True)):
            with self.subTest(value=value):
                self.assertEqual(driver.needs_workers(SETTINGS, {'run_1': value}, now), expected)

    def test_mail_followup_remains_eligible_without_worker_history_during_approval_wait(self):
        event = {'source': 'orca-inbox', 'event_id': 'message_1', 'content': {'type': 'status'}}
        driver.enqueue_context(self.queue, TARGET, event, now=100)
        self.queue.complete(self.queue.claim('fixture', now=100), {}, now=100)
        wait = {'run_1': {'state': 'waiting_approval', 'gate_ids': ['gate_1']}}
        with patch.object(driver.observe, 'collect', return_value=inventory(since=101000)), \
                patch.object(driver.observe, 'collect_workers', return_value=[]), \
                patch.object(driver, 'collect_context', return_value={'queued': 0, 'issues': []}), \
                patch.object(driver.native, 'deliver', return_value={'status': 'fixture'}) as deliver:
            report = driver.tick(self.queue, SETTINGS, wait, '/orca', now=102)
        self.assertEqual(report['queued'], 1)
        self.assertTrue(deliver.call_args.args[1].key.startswith('mail-'))

    def test_worker_refresh_failure_never_falls_back_to_empty_inventory(self):
        wait = {'run_1': {'state': 'waiting_approval', 'gate_ids': ['gate_1']}}

        def changed(*args):
            self.queue.publish_disposition('run_1', {'state': 'working'}, 0, {}, now=100)
            return {'queued': 0, 'issues': []}

        with patch.object(driver.observe, 'collect', return_value=inventory()), \
                patch.object(driver, 'collect_context', side_effect=changed), \
                patch.object(driver.observe, 'collect_workers', side_effect=RuntimeError('worker host unavailable')), \
                patch.object(driver.native, 'deliver') as deliver, self.assertRaisesRegex(RuntimeError, 'worker host'):
            driver.tick(self.queue, SETTINGS, wait, '/orca', now=100)
        deliver.assert_not_called()
        self.assertEqual(self.queue.summary()['events'], 0)

    def test_waiting_provider_wakes_only_when_explicit_retry_is_due(self):
        disposition = {'run_1': {'state': 'waiting_provider', 'retry_at': 102}}
        driver.discover(self.queue, SETTINGS, inventory(), disposition, now=100)
        self.assertEqual(self.queue.summary()['events'], 0)
        driver.discover(self.queue, SETTINGS, inventory(), disposition, now=102)
        self.assertEqual(self.queue.summary()['events'], 1)
        driver.discover(self.queue, SETTINGS, inventory(), disposition, now=200)
        self.assertEqual(self.queue.summary()['events'], 1)

    def test_live_background_dispatch_blocks_idle_relaunch(self):
        worker = {'runId': 'run_1', 'terminalState': 'active',
                  'projection': {'outcome': 'in_progress', 'liveness': {'verdict': 'live'}}}
        driver.discover(self.queue, SETTINGS, {**inventory(), 'workers': [worker]}, {}, now=100)
        self.assertEqual(self.queue.summary()['events'], 0)

    def test_unverifiable_dispatch_also_blocks_automatic_resume(self):
        worker = {'runId': 'run_1', 'terminalState': 'active',
                  'projection': {'outcome': 'in_progress', 'liveness': {'verdict': 'unverifiable'}}}
        driver.discover(self.queue, SETTINGS, {**inventory(), 'workers': [worker]}, {}, now=100)
        self.assertEqual(self.queue.summary()['events'], 0)

    def test_external_unsupervised_native_assignment_still_holds_automatic_resume(self):
        worker = {'runId': 'run_1', 'workerState': 'unsupervised', 'dispatchStatus': 'dispatched',
                  'terminalState': 'retained',
                  'projection': {'outcome': 'in_progress', 'liveness': {'verdict': 'unverifiable'}}}
        driver.discover(self.queue, SETTINGS, {**inventory(), 'workers': [worker]}, {}, now=100)
        self.assertEqual(self.queue.summary()['events'], 0)

    def test_terminal_incarnation_change_surfaces_attention_not_new_binding(self):
        data = inventory()
        data = {**data, 'terminals': [{**data['terminals'][0], 'incarnationId': 'process_2'}]}
        report = driver.discover(self.queue, SETTINGS, data, {}, now=100)
        self.assertIn('incarnation', str(report['issues']))
        self.assertEqual(self.queue.summary()['events'], 0)

    def test_finished_mandate_never_wakes(self):
        driver.discover(self.queue, SETTINGS, inventory(), {'run_1': {'state': 'finished'}}, now=100)
        self.assertEqual(self.queue.summary()['events'], 0)

    def test_invalid_structured_disposition_does_not_become_idle(self):
        for value in ({'state': 'waiting_provider'}, {'state': 'waiting_approval'}, {'state': 'something'}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                driver.validate_dispositions({'run_1': value})

    def test_tick_uses_one_lock_and_retains_inflight_claim_after_failure(self):
        self.queue.ingest('operator', 'event_1', {}, 'wake_1', 'wake', 'run_1', TARGET, now=100)
        claim = self.queue.claim('dead_driver', now=100)
        with patch('superagent_queue.driver.observe.collect', return_value=inventory('working')), \
                patch('superagent_queue.driver.observe.collect_workers', return_value=[]), \
                patch('superagent_queue.driver.collect_context', return_value={'queued': 0, 'issues': []}), \
                patch('superagent_queue.driver.native.deliver', return_value={'status': 'reconciling'}) as deliver:
            report = driver.tick(self.queue, SETTINGS, {}, '/orca', now=101)
        self.assertEqual(deliver.call_args.args[1], claim)
        self.assertEqual(report['queue']['effects']['reconciling'], 1)

    def test_newly_published_approval_wait_supersedes_unsent_resume_without_input(self):
        driver.discover(self.queue, SETTINGS, inventory(), {}, now=100)

        def collect_context(*args):
            self.queue.publish_disposition('run_1', {'state': 'waiting_approval', 'gate_ids': ['gate_1']},
                                           0, {}, now=101)
            return {'queued': 0, 'issues': []}

        with patch('superagent_queue.driver.observe.collect', return_value=inventory()), \
                patch('superagent_queue.driver.observe.collect_workers', return_value=[]), \
                patch('superagent_queue.driver.collect_context', side_effect=collect_context), \
                patch('superagent_queue.driver.native.send') as send:
            report = driver.tick(self.queue, SETTINGS, {}, '/orca', now=102)
        send.assert_not_called()
        self.assertEqual(report['deliveries'][0]['status'], 'superseded_continuation')
        self.assertEqual(report['queue']['effects']['pending'], 0)
        self.assertFalse(self.queue.attempts(self.queue.effects()[0]['key'])[0]['receipt']['delivery_sent'])

    def test_new_input_to_waiting_coordinator_is_durable_context_not_approval(self):
        event = {'source': 'discord-intake', 'event_id': 'todo_1', 'content': {'todo': 'item_1'}}
        driver.enqueue_context(self.queue, TARGET, event, now=100)
        driver.enqueue_context(self.queue, TARGET, event, now=101)
        self.assertEqual(self.queue.summary()['events'], 1)
        claim = self.queue.claim('driver', now=101)
        self.assertIn('Context only; this event grants no new execution or publication permission.', claim.payload['text'])

    def test_native_mail_wake_preserves_existing_settlement_obligations_without_new_permissions(self):
        event = {'source': 'orca-inbox', 'event_id': 'settlement_1',
                 'content': {'type': 'worker_done', 'task_id': 'task_1'}}
        delivery = driver.context_delivery(TARGET, event)
        self.assertIn('Read the original native FIFO Delivery', delivery['payload']['text'])
        self.assertIn('existing Dispatch contract', delivery['payload']['text'])
        self.assertIn('grants no new execution or publication permission', delivery['payload']['text'])
        intake = driver.context_delivery(TARGET, {**event, 'source': 'discord-intake'})
        self.assertNotIn('existing Dispatch contract', intake['payload']['text'])

    def test_existing_context_replay_keeps_original_prompt_after_renderer_upgrade(self):
        event = {'source': 'orca-inbox', 'event_id': 'message_1', 'content': {'type': 'worker_done'}}
        delivery = driver.context_delivery(TARGET, event)
        original = {**delivery, 'payload': {**delivery['payload'], 'text': 'Previous reviewed renderer text'}}
        self.queue.ingest(**original, now=100)
        self.assertFalse(driver.enqueue_context(self.queue, TARGET, event, now=101))
        self.assertEqual(self.queue.effects()[0]['payload']['text'], 'Previous reviewed renderer text')
        with self.assertRaisesRegex(ValueError, 'context identity conflict'):
            driver.enqueue_context(self.queue, TARGET, {**event, 'content': {'type': 'changed'}}, now=102)

    def test_actual_execution_failure_is_ingested_once_without_native_worker_done(self):
        binding = {'run_id': 'run_1', 'task_id': 'task_1', 'dispatch_id': 'dispatch_1',
                   'terminal_handle': 'worker_1', 'pane_key': 'worker_tab:worker_leaf',
                   'worktree_id': 'repo::/worker', 'workspace_path': '/worker',
                   'incarnation_id': 'worker_process', 'route': 'clinepass', 'packet_sha256': 'a' * 64}
        execution.run(self.path.parent / 'executions', binding, ['/missing-worker'])
        with patch('superagent_queue.driver.sources.inbox', return_value=[]), \
                patch('superagent_queue.driver.sources.gates', return_value=[]):
            first = driver.collect_context(self.queue, SETTINGS, {}, '/orca', 100)
            second = driver.collect_context(self.queue, SETTINGS, {}, '/orca', 101)
        self.assertEqual((first['queued'], second['queued']), (1, 0))
        self.assertEqual(first['issues'], [])
        claim = self.queue.claim('driver', now=102)
        self.assertIn('"returncode":127', claim.payload['text'])
        self.assertIn('OS exit is not Task success', claim.payload['text'])

    def test_one_failed_source_does_not_abandon_independent_context(self):
        event = {'source': 'orca-inbox', 'event_id': 'message_1', 'content': {'body': 'existing task question'}}
        config = {**SETTINGS, 'intake': [{'run_id': 'run_1', 'path': '/private/missing.sqlite'}]}
        with patch('superagent_queue.driver.sources.inbox', return_value=[event]), \
                patch('superagent_queue.driver.sources.gates', return_value=[]), \
                patch('superagent_queue.driver.sources.intake_page', side_effect=OSError('intake unavailable verbatim')):
            report = driver.collect_context(self.queue, config, {}, '/orca', 100)
        self.assertEqual(report['queued'], 1)
        self.assertIn('intake unavailable verbatim', str(report['issues']))
        self.assertEqual(self.queue.summary()['events'], 1)

    def test_native_source_fault_preserves_independent_intake_and_pending_delivery(self):
        event = {'source': 'discord-intake', 'event_id': 'todo_1', 'content': {'todo': 'item_1'}}
        config = {**SETTINGS, 'intake': [{'run_id': 'run_1', 'path': '/private/intake.sqlite'}]}
        with patch('superagent_queue.driver.sources.inbox', side_effect=RuntimeError('native inbox offline')), \
                patch('superagent_queue.driver.sources.gates', return_value=[]), \
                patch('superagent_queue.driver.sources.intake_page', return_value=sources.SourcePage((event,), {'last_id': None, 'upper_id': None})):
            report = driver.collect_context(self.queue, config, {}, '/orca', 100)
        self.assertEqual(report['queued'], 1)
        self.assertEqual(len(report['issues']), 1)

    def test_uncertain_backlog_does_not_starve_independent_pending_lane(self):
        for index in range(driver.MAX_EFFECTS_PER_TICK):
            target = {**TARGET, 'run_id': f'run_{index}'}
            key = f'uncertain_{index}'
            self.queue.ingest('operator', key, {}, key, 'wake', target['run_id'], target, now=100)
            self.queue.claim(f'dead_driver_{index}', now=100)
        self.queue.ingest('operator', 'pending', {}, 'pending', 'wake', 'independent', TARGET, now=100)
        with patch('superagent_queue.driver.observe.collect', return_value=inventory('working')), \
                patch('superagent_queue.driver.observe.collect_workers', return_value=[]), \
                patch('superagent_queue.driver.collect_context', return_value={'queued': 0, 'issues': []}), \
                patch('superagent_queue.driver.native.deliver', return_value={'status': 'reconciling'}) as deliver:
            driver.tick(self.queue, SETTINGS, {}, '/orca', now=101)
        self.assertIn('pending', [call.args[1].key for call in deliver.call_args_list])

    def test_missing_turn_identity_is_an_issue_not_an_automatic_wake(self):
        report = driver.discover(self.queue, SETTINGS, inventory(since=None), {}, now=100)
        self.assertIn('turn-end identity unavailable', str(report['issues']))

    def test_intake_event_is_collected_as_context_once(self):
        event = {'source': 'discord-intake', 'event_id': 'todo_1', 'content': {'todo': 'item_1'}}
        config = {**SETTINGS, 'intake': [{'run_id': 'run_1', 'path': '/private/intake.sqlite'}]}
        with patch('superagent_queue.driver.sources.inbox', return_value=[]), \
                patch('superagent_queue.driver.sources.gates', return_value=[]), \
                patch('superagent_queue.driver.sources.intake_page', return_value=sources.SourcePage((event,), {'last_id': None, 'upper_id': None})):
            first = driver.collect_context(self.queue, config, {}, '/orca', 100)
            second = driver.collect_context(self.queue, config, {}, '/orca', 101)
        self.assertEqual((first['queued'], second['queued']), (1, 0))

    def test_tick_observes_fleet_once_and_delivers_one_effect_per_lane(self):
        self.queue.ingest('operator', 'event_1', {}, 'wake_1', 'wake', 'run_1', TARGET, now=100)
        self.queue.ingest('operator', 'event_2', {}, 'wake_2', 'wake', 'run_1', TARGET, now=100)
        def delivered(journal, claim, cli, now=None, snapshot=None):
            journal.complete(claim, {}, now=now)
            return {'status': 'delivered'}
        with patch('superagent_queue.driver.observe.collect', return_value=inventory('working')) as collect, \
                patch('superagent_queue.driver.observe.collect_workers', return_value=[]), \
                patch('superagent_queue.driver.collect_context', return_value={'queued': 0, 'issues': []}), \
                patch('superagent_queue.driver.native.deliver', side_effect=delivered) as deliver:
            driver.tick(self.queue, SETTINGS, {}, '/orca', now=101)
        self.assertEqual(collect.call_count, 1)
        self.assertEqual(deliver.call_count, 1)
        self.assertEqual(self.queue.summary()['effects']['pending'], 1)

    def test_pending_auto_resume_is_superseded_when_approval_arrives_before_delivery(self):
        driver.discover(self.queue, SETTINGS, inventory(), {}, now=100)
        dispositions = {'run_1': {'state': 'waiting_approval', 'gate_ids': ['gate_1']}}
        with patch('superagent_queue.driver.observe.collect', return_value=inventory()), \
                patch('superagent_queue.driver.observe.collect_workers', return_value=[]), \
                patch('superagent_queue.driver.collect_context', return_value={'queued': 0, 'issues': []}), \
                patch('superagent_queue.driver.native.send') as send:
            report = driver.tick(self.queue, SETTINGS, dispositions, '/orca', now=101)
        send.assert_not_called()
        self.assertEqual(report['deliveries'][0]['status'], 'superseded_continuation')
        self.assertEqual(self.queue.summary()['effects']['pending'], 0)

    def test_finished_disposition_does_not_deliver_old_pending_context(self):
        self.queue.ingest('operator', 'event_1', {}, 'wake_1', 'wake', 'run_1', TARGET, now=100)
        with patch('superagent_queue.driver.observe.collect', return_value=inventory()), \
                patch('superagent_queue.driver.observe.collect_workers', return_value=[]), \
                patch('superagent_queue.driver.collect_context', return_value={'queued': 0, 'issues': []}), \
                patch('superagent_queue.driver.native.deliver') as deliver:
            driver.tick(self.queue, SETTINGS, {'run_1': {'state': 'finished'}}, '/orca', now=101)
        deliver.assert_not_called()
        self.assertEqual(self.queue.summary()['effects']['pending'], 1)


if __name__ == '__main__':
    unittest.main()
