"""Obsolete unsent continuation never strands review or conceals uncertain input."""
import json
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from superagent_queue import driver, native
from superagent_queue.store import Journal
from test_superagent_driver import TARGET, SETTINGS, inventory
from test_superagent_native import receipt

WAIT = {'run_1': {'state': 'waiting_job', 'job_ids': ['job_1']}}
QUESTION = {'source': 'orca-inbox', 'event_id': 'question_1',
            'content': {'type': 'question', 'body': 'Review the existing worker question.'}}


class ContinuationTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'private/journal.sqlite3'
        self.journal = Journal(self.path)
        self.addCleanup(self.journal.close)

    def resume(self, now=100):
        driver.discover(self.journal, SETTINGS, inventory(), {}, now=now)
        return self.journal.claim('driver', now=now)

    def tick(self, dispositions, now=102):
        result = subprocess.CompletedProcess([], 0, json.dumps(receipt()), '')
        with patch.object(driver.observe, 'collect', return_value=inventory()), \
                patch.object(driver.observe, 'collect_workers', return_value=[]), \
                patch.object(driver, 'collect_context', return_value={'queued': 0, 'issues': []}), \
                patch.object(native.subprocess, 'run', return_value=result) as send:
            report = driver.tick(self.journal, SETTINGS, dispositions, '/orca', now=now)
        return report, send.call_count

    def test_stale_resume_before_question_survives_reopen_and_unblocks_without_send(self):
        claim = self.resume()
        self.journal.defer(claim, now=100)
        driver.enqueue_context(self.journal, TARGET, QUESTION, now=101)
        with Journal(self.path) as reopened:
            self.assertEqual(reopened.summary()['events'], 2)
        first, first_sends = self.tick(WAIT)
        second, second_sends = self.tick(WAIT, now=103)
        self.assertEqual((first_sends, second_sends), (0, 1))
        self.assertEqual(first['deliveries'][0]['status'], 'superseded_continuation')
        self.assertEqual(second['queue']['effects']['delivered'], 2)
        self.assertEqual(self.journal.open_mail('run_1')[0]['message_id'], 'question_1')
        self.assertEqual(self.journal.summary()['events'], 2)

    def test_finished_run_receives_initial_native_question_but_retains_review_obligation(self):
        driver.enqueue_context(self.journal, TARGET, QUESTION, now=100)
        report, sends = self.tick({'run_1': {'state': 'finished'}})
        self.assertEqual(sends, 1)
        self.assertEqual(report['queue']['effects']['delivered'], 1)
        self.assertEqual(len(self.journal.open_mail('run_1')), 1)
        self.assertEqual(report['queued'], 0)

    def test_finished_run_receives_offload_and_intake_review_without_execution_permission(self):
        for index, source in enumerate(('offload-execution', 'offload-reconciliation', 'offload-artifact',
                                        'offload-cleanup', 'offload-resource', 'discord-intake', 'orca-gates')):
            event = {'source': source, 'event_id': f'context_{index}', 'content': {'review': 'pending'}}
            driver.enqueue_context(self.journal, TARGET, event, now=100)
            report, sends = self.tick({'run_1': {'state': 'finished'}}, now=102 + index)
            self.assertEqual(sends, 1, source)
            self.assertEqual(report['queued'], 0)
            self.assertIn('grants no new execution or publication permission', self.journal.effects()[-1]['payload']['text'])

    def test_supersession_records_exact_current_owner_and_original_decision(self):
        claim = self.resume()
        self.assertTrue(self.journal.supersede_continuation(claim, inventory(), WAIT, now=101))
        proof = self.journal.attempts(claim.key)[0]['receipt']
        self.assertFalse(proof['delivery_sent'])
        self.assertEqual(proof['source'], 'continuation_supersession')
        self.assertEqual(proof['original']['disposition'], {})
        self.assertEqual(proof['current']['disposition'], WAIT['run_1'])
        self.assertEqual(proof['owner']['incarnation_id'], TARGET['incarnation_id'])
        self.assertEqual(self.journal.summary()['events'], 1)
        self.assertFalse(self.journal.supersede_continuation(claim, inventory(), WAIT, now=102))

    def test_current_eligible_resume_is_not_superseded(self):
        claim = self.resume()
        self.assertFalse(self.journal.supersede_continuation(claim, inventory(), {}, now=101))
        self.assertEqual(self.journal.summary()['effects']['running'], 1)

    def test_new_native_turn_end_obsoletes_only_the_original_unsent_resume(self):
        claim = self.resume()
        self.assertTrue(self.journal.supersede_continuation(claim, inventory(since=100000), {}, now=101))
        self.assertEqual(self.journal.attempts(claim.key)[0]['receipt']['current']['turn_ended_at'], 100000)
        driver.discover(self.journal, SETTINGS, inventory(since=100000), {}, now=101)
        self.assertNotEqual(self.journal.claim('driver', now=101).key, claim.key)

    def test_missing_regressed_or_nonidle_turn_end_never_authorizes_supersession(self):
        claim = self.resume()
        for data in (inventory(since=None), inventory(since=True), inventory(since=98000),
                     inventory('working'), inventory('permission'), inventory('unknown')):
            with self.subTest(data=data):
                self.assertFalse(self.journal.supersede_continuation(claim, data, WAIT, now=101))
        self.assertEqual(self.journal.summary()['effects']['running'], 1)

    def test_current_owner_change_does_not_cancel_or_rebind_the_resume(self):
        claim = self.resume()
        data = inventory()
        changed = {**data, 'terminals': [{**data['terminals'][0], 'incarnationId': 'other_process'}]}
        with self.assertRaisesRegex(ValueError, 'incarnation'):
            self.journal.supersede_continuation(claim, changed, WAIT, now=101)
        self.assertEqual(self.journal.summary()['effects']['running'], 1)

    def test_ambiguous_receipted_or_replaced_claim_is_not_superseded(self):
        for mode in ('uncertain', 'receipt', 'generation'):
            with self.subTest(mode=mode), Journal(Path(self.directory.name) / mode / 'journal.sqlite3') as journal:
                driver.discover(journal, SETTINGS, inventory(), {}, now=100)
                claim = journal.claim('driver', now=100)
                if mode == 'uncertain':
                    journal.uncertain(claim, 'response lost after possible input', now=101)
                elif mode == 'receipt':
                    journal.record_receipt(claim, receipt('input_accepted'))
                else:
                    journal.replace(claim.key, {**claim.payload, 'text': 'New continuation intent.'}, now=101)
                self.assertFalse(journal.supersede_continuation(claim, inventory(), WAIT, now=102))
                self.assertEqual(journal.summary()['effects']['delivered'], 0)

    def test_replacement_generation_claim_cannot_reuse_the_original_event_proof(self):
        claim = self.resume()
        self.journal.defer(claim, now=100)
        self.journal.replace(claim.key, {**claim.payload, 'text': 'New intent.'}, now=101)
        replacement = self.journal.claim('driver', now=101)
        self.assertFalse(self.journal.supersede_continuation(replacement, inventory(), WAIT, now=102))

    def test_fake_resume_key_and_raw_operator_wake_are_not_automatic_continuation(self):
        for source in ('operator', 'continuity'):
            with self.subTest(source=source), Journal(Path(self.directory.name) / source / 'journal.sqlite3') as journal:
                journal.ingest(source, 'resume-forged', {}, 'resume-forged', 'wake', 'run_1', TARGET, now=100)
                claim = journal.claim('driver', now=100)
                self.assertFalse(journal.supersede_continuation(claim, inventory(), WAIT, now=101))

    def test_failed_supersession_transaction_preserves_original_attempt_and_fifo(self):
        claim = self.resume()
        driver.enqueue_context(self.journal, TARGET, QUESTION, now=101)
        self.journal.connection.execute('''CREATE TRIGGER reject_finish BEFORE UPDATE ON attempt
            BEGIN SELECT RAISE(ABORT, 'supersession receipt rejected'); END''')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'supersession receipt rejected'):
            self.journal.supersede_continuation(claim, inventory(), WAIT, now=102)
        self.assertEqual(self.journal.attempts(claim.key)[0]['outcome'], 'running')
        self.assertIsNone(self.journal.claim('driver', now=103))

    def test_disposition_change_during_native_owner_refresh_never_sends_old_resume(self):
        claim = self.resume()

        def refresh(*args, **kwargs):
            self.journal.publish_disposition('run_1', WAIT['run_1'], 0, {}, now=101)
            return inventory()

        with patch.object(native.observe, 'collect', side_effect=refresh), patch.object(native, 'send') as send:
            report = native.deliver(self.journal, claim, '/orca', now=102, snapshot=inventory())
        send.assert_not_called()
        self.assertEqual(report['status'], 'superseded_continuation')

    def test_generation_change_during_native_refresh_does_not_send_old_input(self):
        claim = self.resume()

        def refresh(*args, **kwargs):
            self.journal.replace(claim.key, {**claim.payload, 'text': 'New current intent.'}, now=101)
            return inventory()

        with patch.object(native.observe, 'collect', side_effect=refresh), patch.object(native, 'send') as send:
            report = native.deliver(self.journal, claim, '/orca', now=102, snapshot=inventory())
        send.assert_not_called()
        self.assertEqual(report['status'], 'stale_claim')
        self.assertEqual(self.journal.claim('driver', now=102).generation, 2)

    def test_uncertain_resume_still_blocks_later_question_without_blind_resend(self):
        claim = self.resume()
        self.journal.uncertain(claim, 'native request identity lost', now=101)
        driver.enqueue_context(self.journal, TARGET, QUESTION, now=101)
        report, sends = self.tick(WAIT)
        self.assertEqual(sends, 0)
        self.assertEqual(report['queue']['effects']['reconciling'], 1)
        self.assertEqual(report['queue']['effects']['pending'], 1)

    def test_fresh_active_dispatch_blocks_pending_resume_without_duplicate_input(self):
        claim = self.resume()
        data = {**inventory(), 'workers': [{'runId': 'run_1', 'dispatchId': 'dispatch_1',
                 'dispatchStatus': 'dispatched', 'terminalState': 'retained'}]}
        with patch.object(native.observe, 'collect', return_value=data) as collect, patch.object(native, 'send') as send:
            report = native.deliver(self.journal, claim, '/orca', now=101, snapshot=inventory())
        send.assert_not_called()
        collect.assert_called_once_with('/orca', run_ids=('run_1',), include_workers=True)
        self.assertEqual(report['status'], 'superseded_continuation')
        proof = self.journal.attempts(claim.key)[0]['receipt']
        self.assertEqual(proof['blocking_dispatches'][0]['dispatch_id'], 'dispatch_1')

    def test_unknown_or_regressed_turn_end_blocks_old_input_without_cancellation(self):
        claim = self.resume()
        for index, since in enumerate((None, True, 98000, 200000)):
            with self.subTest(since=since), patch.object(native.observe, 'collect', return_value=inventory(since=since)), \
                    patch.object(native, 'send') as send:
                report = native.deliver(self.journal, claim, '/orca', now=101 + index * 3, snapshot=inventory())
            send.assert_not_called()
            self.assertEqual(report['status'], 'deferred')
            claim = self.journal.claim('driver', now=104 + index * 3)

    def test_provider_wait_suppresses_old_resume_but_allows_new_review_context(self):
        claim = self.resume()
        self.journal.defer(claim, now=100)
        driver.enqueue_context(self.journal, TARGET, QUESTION, now=101)
        wait = {'run_1': {'state': 'waiting_provider', 'retry_at': 500}}
        first, sends = self.tick(wait)
        self.assertEqual(sends, 0)
        self.assertEqual(first['deliveries'][0]['status'], 'superseded_continuation')
        second, sends = self.tick(wait, now=103)
        self.assertEqual(sends, 1)
        self.assertEqual(second['queue']['effects']['delivered'], 2)
        self.assertEqual(len(self.journal.open_mail('run_1')), 1)

    def test_receipted_history_is_not_erased_by_a_later_unsent_attempt(self):
        claim = self.resume()
        self.journal.complete(claim, receipt(), now=101)
        self.journal.connection.execute("UPDATE effect SET status='pending' WHERE key=?", (claim.key,))
        later = self.journal.claim('driver', now=102)
        self.assertFalse(self.journal.supersede_continuation(later, inventory(), WAIT, now=103))
        self.assertEqual(self.journal.attempts(claim.key)[0]['receipt'], receipt())

    def test_changed_disposition_generates_one_replacement_resume_after_old_intent_resolves(self):
        claim = self.resume()
        self.journal.defer(claim, now=100)
        current = {'run_1': {'state': 'working', 'reason': 'Existing approved work remains'}}
        first, sends = self.tick(current)
        self.assertEqual(sends, 0)
        self.assertEqual(first['queue']['events'], 2)
        second, sends = self.tick(current, now=103)
        self.assertEqual(sends, 1)
        self.assertEqual(second['queue']['effects']['delivered'], 2)
        third, sends = self.tick(current, now=104)
        self.assertEqual(sends, 0)
        self.assertEqual(third['queue']['events'], 2)

    def test_owner_refresh_failure_does_not_erase_old_resume_or_advance_question(self):
        claim = self.resume()
        driver.enqueue_context(self.journal, TARGET, QUESTION, now=101)
        with patch.object(native.observe, 'collect', side_effect=RuntimeError('scoped owner refresh offline')), \
                patch.object(native, 'send') as send:
            report = native.deliver(self.journal, claim, '/orca', now=102, snapshot=inventory(), dispositions=WAIT)
        send.assert_not_called()
        self.assertEqual(report['status'], 'retry_wait')
        self.assertEqual(self.journal.summary()['effects']['pending'], 2)
        self.assertIsNone(self.journal.claim('driver', now=103))

    def test_prepare_send_preserves_receipted_older_generation_for_reconciliation(self):
        claim = self.resume()
        self.journal.record_receipt(claim, receipt('input_accepted'))
        self.journal.replace(claim.key, {**claim.payload, 'text': 'New revision.'}, now=101)
        self.assertFalse(self.journal.prepare_send(claim, now=102))
        self.assertIsNotNone(self.journal.claim_record(claim))
        self.assertEqual(self.journal.claim_record(claim)['receipt'], receipt('input_accepted'))
        self.assertIsNone(self.journal.claim('driver', now=103))

    def test_finished_does_not_deliver_replaced_or_forged_review_payload(self):
        for mode in ('replacement', 'forged'):
            with self.subTest(mode=mode), Journal(Path(self.directory.name) / mode / 'journal.sqlite3') as journal:
                driver.enqueue_context(journal, TARGET, QUESTION, now=100)
                effect = journal.effects()[0]
                if mode == 'replacement':
                    journal.replace(effect['key'], {**effect['payload'], 'text': 'Different raw wake.'}, now=101)
                else:
                    journal.connection.execute("UPDATE effect SET payload=? WHERE key=?",
                                               (json.dumps(TARGET), effect['key']))
                claim = journal.claim('driver', now=102)
                self.assertTrue(driver.pending_held(journal, claim, inventory(), {'run_1': {'state': 'finished'}}, 102))

    def test_eligible_disposition_after_held_refresh_restores_worker_safety_read(self):
        claim = self.resume()
        self.journal.publish_disposition('run_1', WAIT['run_1'], 0, {}, now=100)

        def refresh(*args, **kwargs):
            self.journal.publish_disposition('run_1', {'state': 'working'}, 1, {}, now=101)
            return inventory()

        worker = {'runId': 'run_1', 'dispatchId': 'dispatch_1', 'dispatchStatus': 'dispatched'}
        with patch.object(native.observe, 'collect', side_effect=refresh), \
                patch.object(native.observe, 'collect_workers', return_value=[worker]) as workers, \
                patch.object(native, 'send') as send:
            report = native.deliver(self.journal, claim, '/orca', now=102, snapshot=inventory())
        send.assert_not_called()
        workers.assert_called_once_with('/orca', run_id='run_1')
        self.assertEqual(report['status'], 'superseded_continuation')
        self.assertEqual(self.journal.attempts(claim.key)[0]['receipt']['blocking_dispatches'][0]['dispatch_id'], 'dispatch_1')

    def test_already_pending_mail_followup_waits_for_provider_retry_without_spending_failure_budget(self):
        driver.enqueue_context(self.journal, TARGET, QUESTION, now=100)
        self.journal.complete(self.journal.claim('driver', now=100), {}, now=100)
        driver.discover(self.journal, SETTINGS, inventory(since=101000), {}, now=102)
        wait = {'run_1': {'state': 'waiting_provider', 'retry_at': 500}}
        report, sends = self.tick(wait, now=103)
        self.assertEqual(sends, 0)
        self.assertEqual(report['queue']['effects']['pending'], 1)
        self.assertEqual(self.journal.effects()[-1]['failures'], 0)

    def test_provider_hold_published_during_native_followup_refresh_prevents_input(self):
        driver.enqueue_context(self.journal, TARGET, QUESTION, now=100)
        self.journal.complete(self.journal.claim('driver', now=100), {}, now=100)
        driver.discover(self.journal, SETTINGS, inventory(since=101000), {}, now=102)
        claim = self.journal.claim('driver', now=102)

        def refresh(*args, **kwargs):
            self.journal.publish_disposition('run_1', {'state': 'waiting_provider', 'retry_at': 500}, 0, {}, now=103)
            return inventory(since=101000)

        with patch.object(native.observe, 'collect', side_effect=refresh), patch.object(native, 'send') as send:
            report = native.deliver(self.journal, claim, '/orca', now=104, snapshot=inventory(since=101000))
        send.assert_not_called()
        self.assertEqual(report['status'], 'deferred')
        self.assertEqual(len(self.journal.open_mail('run_1')), 1)


if __name__ == '__main__':
    unittest.main()
